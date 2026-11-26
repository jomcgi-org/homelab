package adapter

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
)

// A2A v1.0 values, as kagent's gateway uses them (ARCHITECTURE.md "kagent
// speaks A2A v1.0").
const (
	RoleUser       = "ROLE_USER"
	RoleAgent      = "ROLE_AGENT"
	StateCompleted = "TASK_STATE_COMPLETED"
	StateFailed    = "TASK_STATE_FAILED"
)

// JSON-RPC error codes.
const (
	codeParse          = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
	codeInternal       = -32603
)

const maxRequestBytes = 1 << 20

// Responder runs one turn. *Client implements it.
type Responder interface {
	Respond(ctx context.Context, sessionKey, idempotencyKey, input string) (Reply, error)
}

// Part is one A2A message part. The adapter reads and writes text only.
type Part struct {
	Text string `json:"text,omitempty"`
}

// Message is an A2A message.
type Message struct {
	MessageID string `json:"messageId"`
	ContextID string `json:"contextId,omitempty"`
	TaskID    string `json:"taskId,omitempty"`
	Role      string `json:"role"`
	Parts     []Part `json:"parts"`
}

// TaskStatus is an A2A task status.
type TaskStatus struct {
	State     string   `json:"state"`
	Message   *Message `json:"message,omitempty"`
	Timestamp string   `json:"timestamp,omitempty"`
}

// Artifact is an A2A task artifact.
type Artifact struct {
	ArtifactID string `json:"artifactId"`
	Parts      []Part `json:"parts"`
}

// Task is an A2A task.
type Task struct {
	ID        string      `json:"id"`
	ContextID string      `json:"contextId"`
	Status    TaskStatus  `json:"status"`
	History   []*Message  `json:"history,omitempty"`
	Artifacts []*Artifact `json:"artifacts,omitempty"`
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type sendParams struct {
	Message *Message `json:"message"`
}

// Handler serves A2A JSON-RPC SendMessage and runs one turn at a time, as
// kagent's harness executor does for an Actor.
type Handler struct {
	Hermes Responder
	Logger *slog.Logger
	// Now and NewID are fixed in tests.
	Now   func() time.Time
	NewID func() string

	once sync.Once
	turn chan struct{}
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

func (h *Handler) newID() string {
	if h.NewID != nil {
		return h.NewID()
	}
	return uuid.NewString()
}

func (h *Handler) logger() *slog.Logger {
	if h.Logger != nil {
		return h.Logger
	}
	return slog.Default()
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if v := r.Header.Get("A2A-Version"); v != "" && v != "1.0" {
		http.Error(w, "A2A-Version must be 1.0", http.StatusBadRequest)
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, maxRequestBytes+1))
	if err != nil || len(data) > maxRequestBytes {
		writeRPC(w, rpcResponse{Error: &rpcError{Code: codeInvalidRequest, Message: "request body unreadable or too large"}})
		return
	}
	var req rpcRequest
	if err := json.Unmarshal(data, &req); err != nil {
		writeRPC(w, rpcResponse{Error: &rpcError{Code: codeParse, Message: "invalid JSON"}})
		return
	}
	resp := rpcResponse{ID: req.ID}
	switch {
	case req.JSONRPC != "2.0":
		resp.Error = &rpcError{Code: codeInvalidRequest, Message: "jsonrpc must be 2.0"}
	case req.Method != "SendMessage":
		resp.Error = &rpcError{Code: codeMethodNotFound, Message: "only SendMessage is supported"}
	default:
		task, rpcErr := h.send(r.Context(), req.Params)
		if rpcErr != nil {
			resp.Error = rpcErr
		} else {
			resp.Result = map[string]any{"task": task}
		}
	}
	writeRPC(w, resp)
}

func (h *Handler) send(ctx context.Context, raw json.RawMessage) (*Task, *rpcError) {
	var params sendParams
	if err := json.Unmarshal(raw, &params); err != nil || params.Message == nil {
		return nil, &rpcError{Code: codeInvalidParams, Message: "params.message is required"}
	}
	msg := params.Message
	switch {
	case msg.MessageID == "":
		return nil, &rpcError{Code: codeInvalidParams, Message: "message.messageId is required"}
	case msg.Role != RoleUser:
		return nil, &rpcError{Code: codeInvalidParams, Message: "message.role must be ROLE_USER"}
	case msg.TaskID != "":
		// No task waits for input, so there is no task to continue.
		return nil, &rpcError{Code: codeInvalidParams, Message: "message.taskId is not supported"}
	case len(msg.Parts) != 1 || msg.Parts[0].Text == "":
		return nil, &rpcError{Code: codeInvalidParams, Message: "message needs exactly one non-empty text part"}
	}
	contextID := msg.ContextID
	if contextID == "" {
		contextID = h.newID()
	}
	if !ValidSessionKey(contextID) {
		return nil, &rpcError{Code: codeInvalidParams, Message: "message.contextId must be 1 to 256 characters without control characters"}
	}
	task := &Task{ID: h.newID(), ContextID: contextID}
	user := &Message{MessageID: msg.MessageID, ContextID: contextID, TaskID: task.ID, Role: RoleUser, Parts: msg.Parts}
	task.History = []*Message{user}

	// A request that waits for the turn slot stops when its client leaves,
	// so a dropped request never runs later.
	h.once.Do(func() { h.turn = make(chan struct{}, 1) })
	select {
	case h.turn <- struct{}{}:
	case <-ctx.Done():
		return nil, &rpcError{Code: codeInternal, Message: "request canceled before its turn started"}
	}
	reply, err := h.Hermes.Respond(ctx, contextID, idempotencyKey(contextID, msg.MessageID), msg.Parts[0].Text)
	<-h.turn

	agent := &Message{MessageID: h.newID(), ContextID: contextID, TaskID: task.ID, Role: RoleAgent}
	task.Status = TaskStatus{Message: agent, Timestamp: h.now().UTC().Format(time.RFC3339Nano)}
	if err != nil {
		h.logger().ErrorContext(ctx, "hermes turn failed", "context_id", contextID, "task_id", task.ID, "error", err)
		task.Status.State = StateFailed
		agent.Parts = []Part{{Text: "Hermes turn failed"}}
		return task, nil
	}
	task.Status.State = StateCompleted
	agent.Parts = []Part{{Text: reply.Text}}
	task.Artifacts = []*Artifact{{ArtifactID: h.newID(), Parts: []Part{{Text: reply.Text}}}}
	return task, nil
}

// idempotencyKey binds a retried A2A message to its first Hermes result.
// Hermes accepts 1 to 255 visible ASCII characters, so the key is a digest.
func idempotencyKey(contextID, messageID string) string {
	sum := sha256.Sum256([]byte(contextID + "\x00" + messageID))
	return "a2a-" + hex.EncodeToString(sum[:])
}

func writeRPC(w http.ResponseWriter, resp rpcResponse) {
	resp.JSONRPC = "2.0"
	if resp.ID == nil {
		resp.ID = json.RawMessage("null")
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}
