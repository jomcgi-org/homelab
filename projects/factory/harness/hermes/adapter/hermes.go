package adapter

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode"
)

// SessionKeyHeader names the Hermes conversation for memory scoping.
const SessionKeyHeader = "X-Hermes-Session-Key"

const maxHermesResponse = 8 << 20

// ErrNotSettled means Hermes answered with a status that is not terminal,
// such as a turn that waits on an approval. The adapter fails the turn and
// never polls, so a turn never waits on a person.
var ErrNotSettled = errors.New("hermes: response did not settle")

// Client calls the Hermes API server on loopback.
type Client struct {
	BaseURL string
	Key     string
	HTTP    *http.Client
}

// Reply is the answer to one turn.
type Reply struct {
	Text   string
	Status string
}

type responsesRequest struct {
	Input        string `json:"input"`
	Store        bool   `json:"store"`
	Conversation string `json:"conversation"`
}

type responsesBody struct {
	Status string `json:"status"`
	Output []struct {
		Type    string `json:"type"`
		Role    string `json:"role"`
		Phase   string `json:"phase"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	} `json:"output"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// ValidSessionKey reports whether Hermes accepts key in SessionKeyHeader:
// at most 256 characters and no control characters.
func ValidSessionKey(key string) bool {
	if key == "" || len(key) > 256 {
		return false
	}
	for _, r := range key {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}

// Respond runs one turn with a non-streaming /v1/responses call.
//
// sessionKey goes in SessionKeyHeader and also names the `conversation`, so
// the next turn chains to this one: at v0.23.4 the header alone scopes the
// session but does not replay its transcript. idempotencyKey makes a retry of
// the same A2A message return the first result.
func (c *Client) Respond(ctx context.Context, sessionKey, idempotencyKey, input string) (Reply, error) {
	if !ValidSessionKey(sessionKey) {
		return Reply{}, fmt.Errorf("hermes: invalid session key")
	}
	body, err := json.Marshal(responsesRequest{Input: input, Store: true, Conversation: sessionKey})
	if err != nil {
		return Reply{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimSuffix(c.BaseURL, "/")+"/v1/responses", bytes.NewReader(body))
	if err != nil {
		return Reply{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.Key)
	req.Header.Set(SessionKeyHeader, sessionKey)
	if idempotencyKey != "" {
		req.Header.Set("Idempotency-Key", idempotencyKey)
	}
	hc := c.HTTP
	if hc == nil {
		hc = http.DefaultClient
	}
	resp, err := hc.Do(req)
	if err != nil {
		return Reply{}, fmt.Errorf("hermes: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxHermesResponse+1))
	if err != nil {
		return Reply{}, fmt.Errorf("hermes: read response: %w", err)
	}
	if len(data) > maxHermesResponse {
		return Reply{}, fmt.Errorf("hermes: response larger than %d bytes", maxHermesResponse)
	}
	var out responsesBody
	decodeErr := json.Unmarshal(data, &out)
	if resp.StatusCode != http.StatusOK {
		if decodeErr == nil && out.Error != nil && out.Error.Message != "" {
			return Reply{}, fmt.Errorf("hermes: HTTP %d: %.300s", resp.StatusCode, out.Error.Message)
		}
		return Reply{}, fmt.Errorf("hermes: HTTP %d", resp.StatusCode)
	}
	if decodeErr != nil {
		return Reply{}, fmt.Errorf("hermes: decode response: %w", decodeErr)
	}
	switch out.Status {
	case "completed", "incomplete":
		// An incomplete turn ran out of its iteration budget and still holds
		// the agent's wrap-up, which is a usable answer.
	case "failed", "cancelled":
		if out.Error != nil && out.Error.Message != "" {
			return Reply{}, fmt.Errorf("hermes: turn %s: %.300s", out.Status, out.Error.Message)
		}
		return Reply{}, fmt.Errorf("hermes: turn %s", out.Status)
	default:
		return Reply{}, fmt.Errorf("%w: status %q", ErrNotSettled, out.Status)
	}
	var text strings.Builder
	for _, item := range out.Output {
		// Commentary items are progress notes, never the answer.
		if item.Type != "message" || item.Role != "assistant" || item.Phase == "commentary" {
			continue
		}
		for _, part := range item.Content {
			if part.Type == "output_text" {
				text.WriteString(part.Text)
			}
		}
	}
	if text.Len() == 0 {
		return Reply{}, fmt.Errorf("hermes: turn %s without an answer", out.Status)
	}
	return Reply{Text: text.String(), Status: out.Status}, nil
}
