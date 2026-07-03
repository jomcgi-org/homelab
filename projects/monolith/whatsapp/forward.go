package whatsapp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

// InboundPayload is the message the gateway forwards to the monolith inbound
// endpoint (ADR 039 spec section 2). The JSON tags are the wire contract the
// monolith's InboundMessage model reads.
type InboundPayload struct {
	GroupJID        string `json:"group_jid"`
	SenderJID       string `json:"sender_jid"`
	SenderName      string `json:"sender_name"`
	MessageID       string `json:"message_id"`
	Text            string `json:"text"`
	QuotedMessageID string `json:"quoted_message_id,omitempty"`
	Timestamp       string `json:"timestamp"`
}

// Forwarder delivers a forwarded message to the monolith. It is an interface so
// the gateway can be unit-tested with a fake (no HTTP); the production
// implementation is HTTPForwarder.
type Forwarder interface {
	// Forward hands off a payload for delivery. It must preserve per-group
	// ordering and must not block the caller (the whatsmeow event goroutine) on
	// the network; delivery, retry, and backoff happen on a per-group worker.
	Forward(payload InboundPayload)
}

// HTTPForwarder POSTs payloads to the monolith inbound endpoint with a bearer
// token, one ordered worker per group JID so a group's messages arrive in the
// order WhatsApp delivered them. Delivery is at-least-once: a worker retries a
// message with capped exponential backoff until it succeeds or the context is
// cancelled, and does not advance to the group's next message until the current
// one lands (ordering over throughput; the monolith dedupes replays).
type HTTPForwarder struct {
	ctx    context.Context
	url    string
	token  string
	client *http.Client
	log    *slog.Logger

	// Backoff bounds for the per-message retry loop. Defaults are set by
	// NewHTTPForwarder; tests set small values directly.
	initialBackoff time.Duration
	maxBackoff     time.Duration

	mu     sync.Mutex
	queues map[string]chan InboundPayload
}

// forwardQueueDepth buffers a group's pending payloads so a brief monolith blip
// does not block the whatsmeow event goroutine. A sustained outage eventually
// fills it and applies backpressure (a blocking send), which is the correct
// ordering-preserving behaviour for a low-volume household group.
const forwardQueueDepth = 1024

// NewHTTPForwarder builds a production forwarder. The context bounds every
// worker's lifetime (cancelled on shutdown), so a message in a retry loop stops
// cleanly rather than blocking process exit.
func NewHTTPForwarder(ctx context.Context, url, token string, client *http.Client, log *slog.Logger) *HTTPForwarder {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	return &HTTPForwarder{
		ctx:            ctx,
		url:            url,
		token:          token,
		client:         client,
		log:            log,
		initialBackoff: 1 * time.Second,
		maxBackoff:     30 * time.Second,
		queues:         make(map[string]chan InboundPayload),
	}
}

// Forward enqueues the payload onto its group's ordered queue, spawning the
// group's worker on first use. The send blocks only if the group's buffer is
// full (a sustained monolith outage), which is intentional backpressure.
func (f *HTTPForwarder) Forward(payload InboundPayload) {
	ch := f.queueFor(payload.GroupJID)
	select {
	case ch <- payload:
	case <-f.ctx.Done():
	}
}

// queueFor returns the group's channel, lazily creating it and its worker.
func (f *HTTPForwarder) queueFor(groupJID string) chan InboundPayload {
	f.mu.Lock()
	defer f.mu.Unlock()
	if ch, ok := f.queues[groupJID]; ok {
		return ch
	}
	ch := make(chan InboundPayload, forwardQueueDepth)
	f.queues[groupJID] = ch
	go f.worker(ch)
	return ch
}

// worker delivers a single group's payloads sequentially, so the group's WhatsApp
// order is preserved end to end.
func (f *HTTPForwarder) worker(ch chan InboundPayload) {
	for {
		select {
		case <-f.ctx.Done():
			return
		case payload := <-ch:
			f.deliver(payload)
		}
	}
}

// deliver POSTs one payload, retrying with capped exponential backoff until the
// monolith accepts it (2xx) or the context is cancelled. It blocks the group's
// worker until then, which is what preserves ordering under a partial outage.
func (f *HTTPForwarder) deliver(payload InboundPayload) {
	backoff := f.initialBackoff
	for attempt := 1; ; attempt++ {
		err := f.postOnce(payload)
		if err == nil {
			return
		}
		f.log.Warn("whatsapp forward failed; will retry",
			"group_jid", payload.GroupJID,
			"message_id", payload.MessageID,
			"attempt", attempt,
			"err", err,
		)
		select {
		case <-f.ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < f.maxBackoff {
			backoff *= 2
			if backoff > f.maxBackoff {
				backoff = f.maxBackoff
			}
		}
	}
}

// postOnce performs a single authenticated POST. A non-2xx status is an error so
// deliver retries it (the monolith dedupes replays).
func (f *HTTPForwarder) postOnce(payload InboundPayload) error {
	body, err := json.Marshal(payload)
	if err != nil {
		// A marshal failure is not retryable, but returning an error keeps deliver
		// retrying; in practice a struct with string fields never fails to marshal.
		return fmt.Errorf("marshal payload: %w", err)
	}
	req, err := http.NewRequestWithContext(f.ctx, http.MethodPost, f.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+f.token)

	resp, err := f.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	// Drain the body so the connection can be reused.
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("monolith inbound returned %d", resp.StatusCode)
	}
	return nil
}
