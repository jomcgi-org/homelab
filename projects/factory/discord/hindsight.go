package discord

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// Memory is the channel memory the bridge reads before a turn and writes
// after it.
type Memory interface {
	Recall(ctx context.Context, guildID, channelID, query string) ([]string, error)
	Retain(ctx context.Context, guildID, channelID string, at time.Time, content string) error
}

// Hindsight is the hindsight-discord REST API, called with the tenant key.
// Each server has its own bank (discord-<guild_id>, HOME-162 contract rule
// 8) and each channel its own tag, which every recall requires and every
// retain carries (rule 5). Both come from the gateway event.
type Hindsight struct {
	BaseURL string
	Key     func() (string, error)
	HTTP    *http.Client
	// RecallTokens bounds the memories put into one turn.
	RecallTokens int

	mu    sync.Mutex
	banks map[string]bool
}

// BankID is a server's bank.
func BankID(guildID string) string { return "discord-" + guildID }

// ChannelTag is a channel's memory tag.
func ChannelTag(channelID string) string { return "channel:" + channelID }

// DocumentID is a channel's document for one UTC day, which the history
// import (HOME-164) writes too, so live and imported memory share a shape.
func DocumentID(channelID string, at time.Time) string {
	return "discord:" + channelID + ":" + at.UTC().Format(time.DateOnly)
}

// RecallRequest is the body of a recall: only the channel's tag, strictly.
type RecallRequest struct {
	Query     string   `json:"query"`
	Tags      []string `json:"tags"`
	TagsMatch string   `json:"tags_match"`
	MaxTokens int      `json:"max_tokens,omitempty"`
}

// RetainItem is one retained exchange.
type RetainItem struct {
	Content           string     `json:"content"`
	Context           string     `json:"context"`
	DocumentID        string     `json:"document_id"`
	UpdateMode        string     `json:"update_mode"`
	Timestamp         string     `json:"timestamp"`
	Tags              []string   `json:"tags"`
	ObservationScopes [][]string `json:"observation_scopes"`
}

// NewRecall builds the recall for a channel.
func NewRecall(channelID, query string, maxTokens int) RecallRequest {
	return RecallRequest{Query: query, Tags: []string{ChannelTag(channelID)}, TagsMatch: "all_strict", MaxTokens: maxTokens}
}

// NewRetain builds the retained exchange for a channel. Observations stay in
// the channel's scope, so consolidation never mixes channels.
func NewRetain(channelID string, at time.Time, content string) RetainItem {
	tag := ChannelTag(channelID)
	return RetainItem{
		Content:           content,
		Context:           "Discord channel " + channelID,
		DocumentID:        DocumentID(channelID, at),
		UpdateMode:        "append",
		Timestamp:         at.UTC().Format(time.RFC3339),
		Tags:              []string{tag},
		ObservationScopes: [][]string{{tag}},
	}
}

// Recall returns the channel's memories for query.
func (h *Hindsight) Recall(ctx context.Context, guildID, channelID, query string) ([]string, error) {
	if err := h.ensureBank(ctx, guildID); err != nil {
		return nil, err
	}
	var out struct {
		Results []struct {
			Text string `json:"text"`
		} `json:"results"`
	}
	req := NewRecall(channelID, query, h.RecallTokens)
	if err := h.call(ctx, http.MethodPost, "/v1/default/banks/"+BankID(guildID)+"/memories/recall", req, &out); err != nil {
		return nil, err
	}
	texts := make([]string, 0, len(out.Results))
	for _, r := range out.Results {
		texts = append(texts, r.Text)
	}
	return texts, nil
}

// Retain appends an exchange to the channel's document for the day. It
// waits until Hindsight has stored it, so the channel's next turn recalls it
// and a failed retain is an error, not a lost memory.
func (h *Hindsight) Retain(ctx context.Context, guildID, channelID string, at time.Time, content string) error {
	if err := h.ensureBank(ctx, guildID); err != nil {
		return err
	}
	body := map[string]any{"items": []RetainItem{NewRetain(channelID, at, content)}, "async": false}
	return h.call(ctx, http.MethodPost, "/v1/default/banks/"+BankID(guildID)+"/memories", body, nil)
}

// ensureBank creates a server's bank once; PUT is idempotent.
func (h *Hindsight) ensureBank(ctx context.Context, guildID string) error {
	h.mu.Lock()
	done := h.banks[guildID]
	h.mu.Unlock()
	if done {
		return nil
	}
	if err := h.call(ctx, http.MethodPut, "/v1/default/banks/"+BankID(guildID), map[string]any{}, nil); err != nil {
		return err
	}
	h.mu.Lock()
	if h.banks == nil {
		h.banks = map[string]bool{}
	}
	h.banks[guildID] = true
	h.mu.Unlock()
	return nil
}

func (h *Hindsight) call(ctx context.Context, method, path string, in, out any) error {
	key, err := h.Key()
	if err != nil {
		return fmt.Errorf("hindsight: tenant key: %w", err)
	}
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, method, h.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	client := h.HTTP
	if client == nil {
		// A synchronous retain runs the LLM's extraction.
		client = &http.Client{Timeout: 3 * time.Minute}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("hindsight: %s %s: HTTP %d: %s", method, path, resp.StatusCode, clip(string(data), 300))
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}
