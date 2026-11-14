// Package itsaplan is the connector's client for the itsaplan v1.10.0
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
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// ErrNotFound is a 404. On heartbeat and result it means the run left
// pending or this attempt was reclaimed; on events, that the attempt is not
// the current executing one.
var ErrNotFound = errors.New("itsaplan: not found")

// ErrConflict is a 409. On events it means the offset does not continue the
// stored transcript.
var ErrConflict = errors.New("itsaplan: conflict")

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

// Usage is what the last model call of a turn read and wrote.
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
	BaseURL string
	HTTP    *http.Client
	// Keys maps an agent key name to the itsaplan API key that acts as it.
	Keys map[string]string
	// Coordinator names the key used for run lookups and comments.
	Coordinator string
}

func (c *Client) do(ctx context.Context, agent, method, path string, body, out any) error {
	key, ok := c.Keys[agent]
	if !ok {
		return fmt.Errorf("itsaplan: no API key for agent %q", agent)
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
	resp, err := hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return err
	}
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return ErrNotFound
	case resp.StatusCode == http.StatusConflict:
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

func runPath(id int64, op string) string {
	return "/agent-runs/" + strconv.FormatInt(id, 10) + "/" + op
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
