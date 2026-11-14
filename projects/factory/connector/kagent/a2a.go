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

// ErrNotAccepted is KAGENT_SEND_NOT_ACCEPTED: nothing was dispatched, and the
// same message may be sent again.
var ErrNotAccepted = errors.New("kagent: send not accepted")

// ErrTaskNotFound is A2A TaskNotFound (-32001): a missing, expired or hidden
// task or context.
var ErrTaskNotFound = errors.New("kagent: task not found")

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

// TaskStatus is a task's current state.
type TaskStatus struct {
	State   string   `json:"state"`
	Message *Message `json:"message,omitempty"`
}

// Artifact is a task output.
type Artifact struct {
	ArtifactID string         `json:"artifactId"`
	Parts      []Part         `json:"parts"`
	Metadata   map[string]any `json:"metadata,omitempty"`
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

func (c *A2A) call(ctx context.Context, agent AgentRef, method string, params, out any) error {
	rpcSeq.Lock()
	rpcSeq.n++
	id := fmt.Sprintf("c%d", rpcSeq.n)
	rpcSeq.Unlock()
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
		tok, err := c.Tokens.Token()
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 60 * time.Second}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("kagent: HTTP %d: %.200s", resp.StatusCode, data)
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
		}
		return &RPCError{Code: r.Error.Code, Message: r.Error.Message}
	}
	if out != nil {
		return json.Unmarshal(r.Result, out)
	}
	return nil
}

// Send sends one user message with a deterministic message ID and returns
// the task holding it without waiting for the turn. A transport failure, or
// any error other than not-accepted or not-found, is ErrAmbiguous: the
// message may have been dispatched.
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
		if errors.Is(err, ErrNotAccepted) || errors.Is(err, ErrTaskNotFound) {
			return nil, err
		}
		return nil, fmt.Errorf("%w: %v", ErrAmbiguous, err)
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

// ListTasks lists one page of the agent's tasks, narrowed to a context when
// contextID is set, with their history.
func (c *A2A) ListTasks(ctx context.Context, agent AgentRef, contextID, pageToken string) ([]*Task, string, error) {
	params := map[string]any{"pageSize": 100}
	if contextID != "" {
		params["contextId"] = contextID
	}
	if pageToken != "" {
		params["pageToken"] = pageToken
	}
	var out struct {
		Tasks         []*Task `json:"tasks"`
		NextPageToken string  `json:"nextPageToken"`
	}
	if err := c.call(ctx, agent, "ListTasks", params, &out); err != nil {
		return nil, "", err
	}
	return out.Tasks, out.NextPageToken, nil
}

// CancelTask requests cancellation and returns the task as kagent then
// reports it. Cancellation is confirmed only by a terminal state.
func (c *A2A) CancelTask(ctx context.Context, agent AgentRef, taskID string) (*Task, error) {
	var t Task
	if err := c.call(ctx, agent, "CancelTask", map[string]any{"id": taskID}, &t); err != nil {
		return nil, err
	}
	return &t, nil
}
