// Package kagent is the connector's client for kagent v1.0.1: the public A2A
// v1.0 JSON-RPC gateway at /agents/{namespace}/{name} and the gRPC
// SessionService. Both authenticate with the connector's projected
// ServiceAccount token.
package kagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// A2A v1.0 task states as they appear on the JSON wire.
const (
	StateSubmitted     = "TASK_STATE_SUBMITTED"
	StateWorking       = "TASK_STATE_WORKING"
	StateCompleted     = "TASK_STATE_COMPLETED"
	StateFailed        = "TASK_STATE_FAILED"
	StateCanceled      = "TASK_STATE_CANCELED"
	StateRejected      = "TASK_STATE_REJECTED"
	StateInputRequired = "TASK_STATE_INPUT_REQUIRED"
	StateAuthRequired  = "TASK_STATE_AUTH_REQUIRED"
)

// UsageMetadataKey is where kagent attaches one model call's token usage.
const UsageMetadataKey = "kagent.dev/a2a/usage"

// Terminal reports whether a task state is final.
func Terminal(state string) bool {
	switch state {
	case StateCompleted, StateFailed, StateCanceled, StateRejected:
		return true
	}
	return false
}

// Quiescent reports whether a task no longer executes: terminal, or waiting
// for input or authorization.
func Quiescent(state string) bool {
	return Terminal(state) || state == StateInputRequired || state == StateAuthRequired
}

// ErrAmbiguous wraps a send whose outcome is unknown: a transport error or a
// protocol error after dispatch may have happened. Never resend blindly.
var ErrAmbiguous = errors.New("kagent: ambiguous send outcome")

// ErrNotAccepted means kagent dispatched nothing for this call: the
// KAGENT_SEND_NOT_ACCEPTED reason, or UnsupportedOperation while the session
// is busy or in a lifecycle operation. The same message may be sent again.
var ErrNotAccepted = errors.New("kagent: send not accepted")

// ErrRejected is an InvalidRequest or InvalidParams answer: kagent refused the
// message itself, for example a message ID reused with different input.
var ErrRejected = errors.New("kagent: send rejected")

// ErrTaskNotFound is A2A TaskNotFound (-32001): a missing, expired or hidden
// task or context.
var ErrTaskNotFound = errors.New("kagent: task not found")

// ErrResponseTooLarge means the response exceeded the bounded read budget.
// It says nothing about whether a send or cancellation took effect.
var ErrResponseTooLarge = errors.New("kagent: response exceeds 32 MiB budget")

const maxResponseBytes = 32 << 20

// AgentRef names a kagent Agent.
type AgentRef struct {
	Namespace string
	Name      string
}

// Part is one content part. Only text and data parts are produced.
type Part struct {
	Text     string         `json:"text,omitempty"`
	Data     any            `json:"data,omitempty"`
	Metadata map[string]any `json:"metadata,omitempty"`
}

// Message is an A2A message.
type Message struct {
	MessageID string         `json:"messageId"`
	ContextID string         `json:"contextId,omitempty"`
	TaskID    string         `json:"taskId,omitempty"`
	Role      string         `json:"role"`
	Parts     []Part         `json:"parts"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// TaskStatus is a task's current state. Timestamp is when it last changed
// (RFC 3339), when kagent reports it.
type TaskStatus struct {
	State     string   `json:"state"`
	Message   *Message `json:"message,omitempty"`
	Timestamp string   `json:"timestamp,omitempty"`
}

// Artifact is a task output.
type Artifact struct {
	ArtifactID string         `json:"artifactId"`
	Parts      []Part         `json:"parts"`
	Metadata   map[string]any `json:"metadata,omitempty"`
}

// TaskPage retains the coverage metadata needed to prove a complete scan.
// TotalSize counts matching tasks before pagination; it is not the page length.
type TaskPage struct {
	Tasks         []*Task `json:"tasks"`
	TotalSize     int     `json:"totalSize"`
	PageSize      int     `json:"pageSize"`
	NextPageToken string  `json:"nextPageToken"`
}

// Task is an A2A task with its synthesized history.
type Task struct {
	ID        string         `json:"id"`
	ContextID string         `json:"contextId"`
	Status    TaskStatus     `json:"status"`
	History   []*Message     `json:"history,omitempty"`
	Artifacts []*Artifact    `json:"artifacts,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

// kagent v1.0.1 records each Codex tool or command as its own artifact: a
// data part {id, name, args} marked function_call when Codex reports the
// item started, and a function_response part with the same id when it
// completes or the turn ends
// (https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/harness/codex/internal/driver/events.go#L360-L394,
// https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/api/a2a/tool_activity.go#L8-L26).
const (
	PartTypeKey      = "kagent.dev/a2a/part-type"
	PartToolCall     = "function_call"
	PartToolResponse = "function_response"
)

// ToolRunning reports whether a tool call in the task has no result yet:
// a tool or command is still running.
func (t *Task) ToolRunning() bool {
	open := map[string]bool{}
	for _, a := range t.Artifacts {
		if a == nil {
			continue
		}
		for _, p := range a.Parts {
			data, _ := p.Data.(map[string]any)
			id, _ := data["id"].(string)
			switch p.Metadata[PartTypeKey] {
			case PartToolCall:
				open[id] = true
			case PartToolResponse:
				delete(open, id)
			}
		}
	}
	return len(open) > 0
}

// HasMessage reports whether the task's history holds the message ID.
func (t *Task) HasMessage(id string) bool {
	for _, m := range t.History {
		if m != nil && m.MessageID == id {
			return true
		}
	}
	return false
}

// TokenSource reads the projected ServiceAccount token, re-reading the file
// as the kubelet rotates it.
type TokenSource struct {
	Path string

	mu      sync.Mutex
	token   string
	readAt  time.Time
	refresh time.Duration
}

// Token returns the current bearer token.
func (s *TokenSource) Token() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	refresh := s.refresh
	if refresh == 0 {
		refresh = time.Minute
	}
	if s.token != "" && time.Since(s.readAt) < refresh {
		return s.token, nil
	}
	b, err := os.ReadFile(s.Path)
	if err != nil {
		return "", fmt.Errorf("kagent: read token: %w", err)
	}
	s.token = strings.TrimSpace(string(b))
	s.readAt = time.Now()
	return s.token, nil
}

// DefaultTimeout bounds one A2A call. The connector's resend window must
// exceed it plus kagent's two-minute dispatch reservation.
const DefaultTimeout = 60 * time.Second

// CancelTaskTimeout bounds one CancelTask. kagent forwards it to the
// session's runtime, which can hang; the connector deletes a session whose
// cancellation kagent does not confirm, so a long wait gains nothing
// (HOME-141).
const CancelTaskTimeout = 15 * time.Second

// A2A calls the public A2A gateway.
type A2A struct {
	BaseURL string
	HTTP    *http.Client
	Tokens  *TokenSource
}

type rpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      string `json:"id"`
	Method  string `json:"method"`
	Params  any    `json:"params"`
}

type rpcError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

type rpcResponse struct {
	Result json.RawMessage `json:"result"`
	Error  *rpcError       `json:"error"`
}

// RPCError is a JSON-RPC error kagent returned.
type RPCError struct {
	Code    int
	Message string
}

func (e *RPCError) Error() string { return fmt.Sprintf("kagent: rpc %d: %s", e.Code, e.Message) }

var rpcSeq struct {
	sync.Mutex
	n int64
}

type requestIDKey struct{}

// WithRequestID binds a journaled read to the same JSON-RPC ID across retries.
// It correlates attempts without caching responses or changing SendMessage IDs.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

func (c *A2A) call(ctx context.Context, agent AgentRef, method string, params, out any) error {
	var project func(io.Reader) ([]byte, error)
	if method == "ListTasks" {
		project = taskMetadataJSON
	}
	return c.callProjected(ctx, agent, method, params, out, project)
}

func (c *A2A) callProjected(ctx context.Context, agent AgentRef, method string, params, out any, project func(io.Reader) ([]byte, error)) error {
	rpcSeq.Lock()
	rpcSeq.n++
	id := fmt.Sprintf("c%d", rpcSeq.n)
	rpcSeq.Unlock()
	if recorded, ok := ctx.Value(requestIDKey{}).(string); ok && recorded != "" {
		id = recorded
	}
	body, err := json.Marshal(rpcRequest{JSONRPC: "2.0", ID: id, Method: method, Params: params})
	if err != nil {
		return err
	}
	url := strings.TrimSuffix(c.BaseURL, "/") + "/agents/" + agent.Namespace + "/" + agent.Name
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("A2A-Version", "1.0")
	if c.Tokens != nil {
		tok, err := tokenForContext(ctx, c.Tokens)
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: DefaultTimeout}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var data []byte
	if project != nil && resp.StatusCode == http.StatusOK {
		data, err = project(resp.Body)
	} else {
		data, err = io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	}
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("kagent: HTTP %d: %.200s", resp.StatusCode, data)
	}
	if len(data) > maxResponseBytes {
		return fmt.Errorf("%w: %s", ErrResponseTooLarge, method)
	}
	var r rpcResponse
	if err := json.Unmarshal(data, &r); err != nil {
		return fmt.Errorf("kagent: decode response: %w", err)
	}
	if r.Error != nil {
		switch {
		case bytes.Contains(r.Error.Data, []byte("KAGENT_SEND_NOT_ACCEPTED")):
			return fmt.Errorf("%w: %s", ErrNotAccepted, r.Error.Message)
		case r.Error.Code == -32001:
			return fmt.Errorf("%w: %s", ErrTaskNotFound, r.Error.Message)
		case method == "SendMessage" && r.Error.Code == -32004:
			return fmt.Errorf("%w: %s", ErrNotAccepted, r.Error.Message)
		case method == "SendMessage" && (r.Error.Code == -32600 || r.Error.Code == -32602):
			return fmt.Errorf("%w: %s", ErrRejected, r.Error.Message)
		}
		return &RPCError{Code: r.Error.Code, Message: r.Error.Message}
	}
	if out != nil {
		return json.Unmarshal(r.Result, out)
	}
	return nil
}

// Send sends one user message with a deterministic message ID and returns
// the task holding it without waiting for the turn. kagent accepts a
// task-opening message (no task ID) once per session and answers a repeat
// with the task that holds it, so sending the same message again is how an
// ambiguous outcome is resolved. A transport failure, or any error other
// than not-accepted, rejected or not-found, is ErrAmbiguous.
func (c *A2A) Send(ctx context.Context, agent AgentRef, msg Message) (*Task, error) {
	msg.Role = "ROLE_USER"
	params := map[string]any{
		"message":       msg,
		"configuration": map[string]any{"returnImmediately": true},
	}
	var out struct {
		Task    *Task    `json:"task"`
		Message *Message `json:"message"`
	}
	if err := c.call(ctx, agent, "SendMessage", params, &out); err != nil {
		if errors.Is(err, ErrNotAccepted) || errors.Is(err, ErrTaskNotFound) || errors.Is(err, ErrRejected) {
			return nil, err
		}
		return nil, fmt.Errorf("%w: %w", ErrAmbiguous, err)
	}
	if out.Task == nil {
		return nil, fmt.Errorf("%w: send answered without a task", ErrAmbiguous)
	}
	return out.Task, nil
}

// GetTask reads a task's committed state and history.
func (c *A2A) GetTask(ctx context.Context, agent AgentRef, taskID string) (*Task, error) {
	var t Task
	if err := c.call(ctx, agent, "GetTask", map[string]any{"id": taskID}, &t); err != nil {
		return nil, err
	}
	return &t, nil
}

// GetTaskMetadata reads the exact task without retaining its output. Pinned
// kagent includes artifacts even with history disabled, so stream and validate
// them while discarding their contents. The metadata budget applies after this
// projection; the deadline bounds the whole read. No unrelated task is read.
func (c *A2A) GetTaskMetadata(ctx context.Context, agent AgentRef, taskID string) (*Task, error) {
	ctx, cancel := context.WithTimeout(ctx, DefaultTimeout)
	defer cancel()
	var out Task
	if err := c.callProjected(ctx, agent, "GetTask", map[string]any{"id": taskID, "historyLength": 0}, &out, exactTaskMetadataJSON); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListTasks lists metadata without history or artifacts: one session's, or
// with no contextID, those of every session of the Agent the caller owns.
// GetTask includes artifacts even with historyLength: 0 in pinned kagent.
func (c *A2A) ListTasks(ctx context.Context, agent AgentRef, contextID, state, pageToken string) (*TaskPage, error) {
	// The projection bounds retained bytes; the deadline also bounds reading
	// and discarding arbitrarily large status messages on the wire.
	ctx, cancel := context.WithTimeout(ctx, DefaultTimeout)
	defer cancel()
	params := map[string]any{"pageSize": 100, "historyLength": 0, "includeArtifacts": false}
	if contextID != "" {
		params["contextId"] = contextID
	}
	if state != "" {
		params["status"] = state
	}
	if pageToken != "" {
		params["pageToken"] = pageToken
	}
	var out *struct {
		Tasks         *[]*Task `json:"tasks"`
		TotalSize     *int     `json:"totalSize"`
		PageSize      *int     `json:"pageSize"`
		NextPageToken *string  `json:"nextPageToken"`
	}
	if err := c.call(ctx, agent, "ListTasks", params, &out); err != nil {
		return nil, err
	}
	// kagent's pinned A2A SDK emits all four fields, including tasks: [] for
	// an empty page. Missing/null fields are uncertainty, not task absence.
	if out == nil || out.Tasks == nil || out.TotalSize == nil || out.PageSize == nil || out.NextPageToken == nil ||
		*out.TotalSize < 0 || *out.PageSize < 1 || *out.PageSize > 100 ||
		len(*out.Tasks) > *out.PageSize || (*out.NextPageToken != "" && len(*out.Tasks) != *out.PageSize) {
		return nil, errors.New("kagent: invalid task list response")
	}
	// Count and rows use separate database queries. Preserve their metadata:
	// the caller may adopt an exact task even if counts raced, but must not
	// infer absence until a complete scan accounts for the advertised total.
	return &TaskPage{Tasks: *out.Tasks, TotalSize: *out.TotalSize, PageSize: *out.PageSize, NextPageToken: *out.NextPageToken}, nil
}

// CancelTask requests cancellation and returns the task as kagent then
// reports it. Cancellation is confirmed only by a terminal state.
func (c *A2A) CancelTask(ctx context.Context, agent AgentRef, taskID string) (*Task, error) {
	ctx, cancel := context.WithTimeout(ctx, CancelTaskTimeout)
	defer cancel()
	var t Task
	if err := c.call(ctx, agent, "CancelTask", map[string]any{"id": taskID}, &t); err != nil {
		return nil, err
	}
	return &t, nil
}
