// Package itsaplan is the factory's client for the itsaplan v1.10.0
// external-agent API: the runner routes a coding agent's key drains
// (claim, heartbeat, events, result) and the reads and comments the
// coordinator agent's key makes.
package itsaplan

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"
)

// ErrNotFound is a 404. On heartbeat and result it means the run left
// pending or this attempt was reclaimed; on events, that the attempt is not
// the current executing one.
var ErrNotFound = errors.New("itsaplan: not found")

// ErrConflict is a 409. On events it means the offset does not continue the
// stored transcript.
var ErrConflict = errors.New("itsaplan: conflict")

// ErrWIPLimit is a conflict that can clear when the target column has space.
// Keep ErrConflict in its chain for callers that do not wait for capacity.
var ErrWIPLimit = fmt.Errorf("%w: wip_limit_exceeded", ErrConflict)

// HTTPError carries any other non-2xx answer.
type HTTPError struct {
	Status int
	Body   string
}

func (e *HTTPError) Error() string { return fmt.Sprintf("itsaplan: HTTP %d: %s", e.Status, e.Body) }

// Run is a claimed run (RunnerRunResponse). Attempts is the attempt number
// this claim holds.
type Run struct {
	ID              int64  `json:"id"`
	Trigger         string `json:"trigger"`
	Prompt          string `json:"prompt"`
	SystemPrompt    string `json:"systemPrompt"`
	Attempts        int    `json:"attempts"`
	TraceEvents     bool   `json:"traceEvents"`
	IssueID         *int64 `json:"issueId"`
	IssueIdentifier string `json:"issueIdentifier"`
}

// Usage is the sum of all measured model calls in a run, including retries.
// InputTokens includes cached input tokens.
type Usage struct {
	InputTokens  int64 `json:"inputTokens"`
	OutputTokens int64 `json:"outputTokens"`
}

// Result finishes a run. A failure is never retried by itsaplan.
type Result struct {
	Attempt int    `json:"attempt"`
	Status  string `json:"status"` // success | failed
	Output  string `json:"output,omitempty"`
	Error   string `json:"error,omitempty"`
	Usage   *Usage `json:"usage,omitempty"`
}

// Event is one AG-UI event. Only the fields the connector emits are modelled.
type Event struct {
	Type       string `json:"type"`
	MessageID  string `json:"messageId,omitempty"`
	Role       string `json:"role,omitempty"`
	Delta      string `json:"delta,omitempty"`
	ToolCallID string `json:"toolCallId,omitempty"`
	ToolName   string `json:"toolCallName,omitempty"`
	Content    string `json:"content,omitempty"`
	Message    string `json:"message,omitempty"`
	Code       string `json:"code,omitempty"`
}

// RunStatus is a run as the queue reports it.
type RunStatus struct {
	ID             int64  `json:"id"`
	Status         string `json:"status"`         // pending | success | failed | canceled
	ExecutionState string `json:"executionState"` // queued | running | success | failed | canceled
	Attempts       int    `json:"attempts"`
	LastError      string `json:"lastError"`
}

// Client calls one itsaplan API with a set of agent keys.
type Client struct {
	Limits  *RequestLimits
	BaseURL string
	HTTP    *http.Client
	// Keys maps an agent key name to the itsaplan API key that acts as it.
	Keys map[string]string
	// KeyFunc, when set, replaces Keys: it is asked for the key on every
	// call, so a rotated key takes effect without a restart.
	KeyFunc func(agent string) (string, error)
	// Coordinator names the key used for run lookups and comments.
	Coordinator string
}

// RequestLimits is shared by every client using the same agent credentials.
type (
	RequestLimits struct{ agents sync.Map }
	keyWindow     struct {
		turn   chan struct{}
		burst  int
		resume time.Time
		failed bool
		delay  time.Duration
	}
)

func (l *RequestLimits) enter(ctx context.Context, agent string) (*keyWindow, error) {
	v, _ := l.agents.LoadOrStore(agent, &keyWindow{turn: make(chan struct{}, 1)})
	w := v.(*keyWindow)
	select {
	case w.turn <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	if wait := time.Until(w.resume); wait > 0 {
		if w.failed {
			<-w.turn
			return nil, ErrBackoff
		}
		timer := time.NewTimer(wait)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-ctx.Done():
			<-w.turn
			return nil, ctx.Err()
		}
	}
	return w, nil
}

func (w *keyWindow) finish(status int) {
	defer func() { <-w.turn }()
	w.burst++
	if status == 0 || status == 429 || status >= 500 {
		w.failed = true
		w.delay = min(time.Minute, max(2*time.Second, 2*w.delay))
		w.resume = time.Now().Add(w.delay/2 + time.Duration(rand.Int64N(int64(w.delay/2))))
		w.burst = 0
		return
	}
	w.delay = 0
	w.failed = false
	// better-auth resets after an idle interval, even at modest throughput.
	if w.burst >= 12 {
		w.burst = 0
		w.resume = time.Now().Add(1100 * time.Millisecond)
	}
}

// ErrBackoff means no request was sent during the cooldown.
var ErrBackoff = fmt.Errorf("%w: backing off", errUnavailable)

func IsUnavailable(err error) bool {
	return errors.Is(err, errUnavailable)
}

var errUnavailable = errors.New("itsaplan unavailable")

func (e *HTTPError) Is(target error) bool {
	return target == errUnavailable && (e.Status == 429 || e.Status >= 500)
}

func (c *Client) key(agent string) (string, error) {
	if c.KeyFunc != nil {
		k, err := c.KeyFunc(agent)
		if err != nil {
			return "", fmt.Errorf("itsaplan: API key for agent %q: %w", agent, err)
		}
		return k, nil
	}
	k, ok := c.Keys[agent]
	if !ok {
		return "", fmt.Errorf("itsaplan: no API key for agent %q", agent)
	}
	return k, nil
}

func (c *Client) do(ctx context.Context, agent, method, path string, body, out any) error {
	key, err := c.key(agent)
	if err != nil {
		return err
	}
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("x-api-key", key)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	status := 0
	if c.Limits != nil {
		window, err := c.Limits.enter(ctx, agent)
		if err != nil {
			return err
		}
		defer func() { window.finish(status) }()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	resp, err := hc.Do(req)
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return ctx.Err()
		}
		return errors.Join(errUnavailable, err)
	}
	status = resp.StatusCode
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		status = 0
		if errors.Is(ctx.Err(), context.Canceled) {
			return ctx.Err()
		}
		return errors.Join(errUnavailable, err)
	}
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return ErrNotFound
	case resp.StatusCode == http.StatusConflict:
		var answer struct {
			Code string `json:"code"`
		}
		if json.Unmarshal(data, &answer) == nil && answer.Code == "wip_limit_exceeded" {
			return ErrWIPLimit
		}
		return fmt.Errorf("%w: %s", ErrConflict, truncate(string(data), 200))
	case resp.StatusCode < 200 || resp.StatusCode > 299:
		return &HTTPError{Status: resp.StatusCode, Body: truncate(string(data), 200)}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

// Claim takes the agent's next queued run, or nil when it has none.
func (c *Client) Claim(ctx context.Context, agent string) (*Run, error) {
	var out struct {
		Run *Run `json:"run"`
	}
	if err := c.do(ctx, agent, http.MethodPost, "/agent-runs/claim", nil, &out); err != nil {
		return nil, err
	}
	return out.Run, nil
}

// Heartbeat extends the lease of one attempt. It always sends the attempt,
// so a reclaimed attempt answers ErrNotFound instead of extending.
func (c *Client) Heartbeat(ctx context.Context, agent string, runID int64, attempt int) error {
	return c.do(ctx, agent, http.MethodPost, runPath(runID, "heartbeat"),
		map[string]int{"attempt": attempt}, nil)
}

// Events appends a batch at offset and returns the next offset. A batch the
// server already holds is acknowledged again; a gap is ErrConflict.
func (c *Client) Events(ctx context.Context, agent string, runID int64, attempt, offset int, events []Event) (int, error) {
	var out struct {
		NextOffset int  `json:"nextOffset"`
		Truncated  bool `json:"truncated"`
	}
	body := map[string]any{"attempt": attempt, "offset": offset, "events": events}
	if err := c.do(ctx, agent, http.MethodPost, runPath(runID, "events"), body, &out); err != nil {
		return 0, err
	}
	return out.NextOffset, nil
}

// Result reports how the attempt ended.
func (c *Client) Result(ctx context.Context, agent string, runID int64, r Result) error {
	return c.do(ctx, agent, http.MethodPost, runPath(runID, "result"), r, nil)
}

// GetRun looks a run up in the runner queue with the coordinator's key. A run
// the queue does not show, including one whose issue moved to another project
// (which cancels it), is ErrNotFound.
func (c *Client) GetRun(ctx context.Context, runID int64) (*RunStatus, error) {
	var out struct {
		Items []RunStatus `json:"items"`
	}
	q := url.Values{"runId": {strconv.FormatInt(runID, 10)}}
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/agent-runs/queue?"+q.Encode(), nil, &out); err != nil {
		return nil, err
	}
	for i := range out.Items {
		if out.Items[i].ID == runID {
			return &out.Items[i], nil
		}
	}
	return nil, ErrNotFound
}

// Comment posts a comment on an issue as the coordinator.
func (c *Client) Comment(ctx context.Context, issueID int64, body string) error {
	return c.do(ctx, c.Coordinator, http.MethodPost,
		"/issues/"+strconv.FormatInt(issueID, 10)+"/comments", map[string]string{"body": body}, nil)
}

// Issue is the part of an itsaplan issue the connector reads.
type Issue struct {
	StartDate      string            `json:"startDate"`
	LabelIDs       []int64           `json:"labelIds"`
	ID             int64             `json:"id"`
	ProjectID      int64             `json:"projectId"`
	Identifier     string            `json:"identifier"`
	Title          string            `json:"title"`
	Description    string            `json:"description"`
	ColumnID       int64             `json:"columnId"`
	Checklists     []Checklist       `json:"checklists"`
	Subtasks       []IssueRef        `json:"subtasks"`
	Parent         *IssueRef         `json:"parent"`
	Links          []IssueLink       `json:"links"`
	Development    []DevelopmentLink `json:"development"`
	AssigneeUserID string            `json:"assigneeUserId"`
	DelegateUserID *string           `json:"delegateUserId"`
	CreatedAt      string            `json:"createdAt"`
	ArchivedAt     *time.Time        `json:"archivedAt"`
}

// Checklist and its items arrive in display order from GET /issues/<id>.
type Checklist struct {
	ID    int64           `json:"id"`
	Title string          `json:"title"`
	Items []ChecklistItem `json:"items"`
}

type ChecklistItem struct {
	ID       int64   `json:"id"`
	Content  string  `json:"content"`
	Done     bool    `json:"done"`
	Position float64 `json:"position"`
}

// IssueRef names a parent, sub-task or linked issue with its current column.
type IssueRef struct {
	ID         int64  `json:"id"`
	Identifier string `json:"identifier"`
	Title      string `json:"title"`
	ColumnID   int64  `json:"columnId"`
	Archived   bool   `json:"archived"`
}

// IssueLink is the relation as read from this issue. In v1.10.0,
// blocked_by is stored as blocks and returned with an inward direction.
type IssueLink struct {
	Kind      string   `json:"kind"`
	Direction string   `json:"direction"`
	Issue     IssueRef `json:"issue"`
}

// BlockedBy excludes outward, unrelated and undirected links.
func (l IssueLink) BlockedBy() bool { return l.Kind == "blocks" && l.Direction == "inward" }

// DevelopmentLink retains only fields that prompts can reduce to fixed classes
// and numbers. Provider text has no trusted reviewer provenance in itsaplan.
type DevelopmentLink struct {
	Kind   string `json:"kind"`
	Number int    `json:"number"`
	State  string `json:"state"`
	Draft  bool   `json:"draft"`
}

// Attachment retains only the name. The connector never fetches its content.
type Attachment struct {
	Filename string `json:"filename"`
}

// IssueRun is the identity and trigger of a recorded run, including terminal
// runs. Prompts, results and errors are deliberately not decoded or forwarded.
type IssueRun struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	Trigger  string `json:"trigger"`
}

type IssueRunPage struct {
	Items      []IssueRun `json:"items"`
	NextCursor *int64     `json:"nextCursor"`
}

// IssueRuns reads one page so dispatch can fence and journal every request.
func (c *Client) IssueRuns(ctx context.Context, issueID, before int64) (*IssueRunPage, error) {
	q := url.Values{"limit": {"50"}}
	if before != 0 {
		q.Set("before", strconv.FormatInt(before, 10))
	}
	var page IssueRunPage
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/"+strconv.FormatInt(issueID, 10)+"/agent-runs?"+q.Encode(), nil, &page); err != nil {
		return nil, err
	}
	return &page, nil
}

// BlockIssue creates a directional dependency. itsaplan answers 409 for a
// duplicate; the outbox re-reads the source's links before retrying.
func (c *Client) BlockIssue(ctx context.Context, source, target int64) error {
	return c.do(ctx, c.Coordinator, http.MethodPost, "/issues/"+strconv.FormatInt(source, 10)+"/links",
		map[string]any{"targetIssueId": target, "kind": "blocks"}, nil)
}

// GetIssue reads an issue with the coordinator's key.
func (c *Client) GetIssue(ctx context.Context, issueID int64) (*Issue, error) {
	var out Issue
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/"+strconv.FormatInt(issueID, 10), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetAttachments lists attachment names with the coordinator's key.
func (c *Client) GetAttachments(ctx context.Context, issueID int64) ([]Attachment, error) {
	var out []Attachment
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/"+strconv.FormatInt(issueID, 10)+"/attachments", nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// Column is a project's workflow state.
type Column struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	StateType string `json:"stateType"` // backlog | unstarted | started | completed | canceled
	Position  int    `json:"position"`
}

// ProjectColumns lists a project's columns, read with the coordinator's key.
func (c *Client) ProjectColumns(ctx context.Context, projectKey string) ([]Column, error) {
	var out struct {
		Columns []Column `json:"columns"`
	}
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/projects/"+url.PathEscape(projectKey), nil, &out); err != nil {
		return nil, err
	}
	return out.Columns, nil
}

// MoveIssue moves an issue to a column as the coordinator. It needs
// work_items edit in the issue's project.
func (c *Client) MoveIssue(ctx context.Context, issueID, columnID int64) error {
	return c.do(ctx, c.Coordinator, http.MethodPatch, "/issues/"+strconv.FormatInt(issueID, 10),
		map[string]int64{"columnId": columnID}, nil)
}

func runPath(id int64, op string) string {
	return "/agent-runs/" + strconv.FormatInt(id, 10) + "/" + op
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
