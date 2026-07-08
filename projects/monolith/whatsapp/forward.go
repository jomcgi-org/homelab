package whatsapp

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
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

// ReactionPayload is a human reaction on one of Bosun's own messages, forwarded
// to the monolith reaction endpoint as the /improve-ambient ground-truth signal.
// The gateway only forwards reactions whose target was a bot-sent message; an
// empty Emoji is WhatsApp's representation of a removed reaction. The JSON tags
// are the wire contract the monolith's ReactionInbound model reads.
type ReactionPayload struct {
	GroupJID        string `json:"group_jid"`
	ReactorJID      string `json:"reactor_jid"`
	TargetMessageID string `json:"target_message_id"`
	Emoji           string `json:"emoji"`
	Timestamp       string `json:"timestamp"`
}

// Forwarder delivers forwarded events to the monolith. It is an interface so the
// gateway can be unit-tested with a fake (no HTTP); the production implementation
// is HTTPForwarder.
type Forwarder interface {
	// Forward hands off a message payload for delivery. It must preserve
	// per-group ordering and must not block the caller (the whatsmeow event
	// goroutine) on the network; delivery, retry, and backoff happen on a
	// per-group worker.
	Forward(payload InboundPayload)
	// ForwardReaction hands off a reaction payload for delivery, through the same
	// per-group ordered worker as Forward so a react/un-react pair keeps its
	// order relative to the group's other traffic.
	ForwardReaction(payload ReactionPayload)
}

// queuedPost is one authenticated POST bound to a group's ordered worker. Both
// message and reaction deliveries flow through the same per-group queue (keyed by
// groupJID) so a group's events keep their WhatsApp order end to end; each post
// carries its own target URL and pre-marshalled body.
type queuedPost struct {
	groupJID string
	url      string
	body     []byte
	// logKind/logID label retry warnings without re-decoding body.
	logKind string
	logID   string
}

// HTTPForwarder POSTs payloads to the monolith inbound endpoint with a bearer
// token, one ordered worker per group JID so a group's messages arrive in the
// order WhatsApp delivered them. Delivery is at-least-once: a worker retries a
// message with capped exponential backoff until it succeeds or the context is
// cancelled, and does not advance to the group's next message until the current
// one lands (ordering over throughput; the monolith dedupes replays).
type HTTPForwarder struct {
	ctx         context.Context
	inboundURL  string
	reactionURL string
	token       string
	client      *http.Client
	log         *slog.Logger

	// Backoff bounds for the per-message retry loop. Defaults are set by
	// NewHTTPForwarder; tests set small values directly.
	initialBackoff time.Duration
	maxBackoff     time.Duration

	mu     sync.Mutex
	queues map[string]chan queuedPost
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
		ctx:         ctx,
		inboundURL:  url,
		reactionURL: reactionURLFrom(url),
		token:       token,
		client:      client,
		log:         log,

		initialBackoff: 1 * time.Second,
		maxBackoff:     30 * time.Second,
		queues:         make(map[string]chan queuedPost),
	}
}

// reactionURLFrom derives the sibling reaction endpoint from the inbound URL. The
// monolith mounts both under the same /internal/whatsapp prefix (.../inbound and
// .../reaction), so swapping the trailing path segment keeps the two in lockstep
// without a second env var to plumb and validate. A URL that does not end in
// /inbound (e.g. a test server root) just gets /reaction appended.
func reactionURLFrom(inboundURL string) string {
	return strings.TrimSuffix(inboundURL, "/inbound") + "/reaction"
}

// Forward enqueues a message payload onto its group's ordered queue.
func (f *HTTPForwarder) Forward(payload InboundPayload) {
	body, err := json.Marshal(payload)
	if err != nil {
		// String-only structs never fail to marshal; drop rather than spin.
		f.log.Error("whatsapp forward: drop unmarshalable message",
			"group_jid", payload.GroupJID, "message_id", payload.MessageID, "err", err)
		return
	}
	f.enqueue(queuedPost{
		groupJID: payload.GroupJID,
		url:      f.inboundURL,
		body:     body,
		logKind:  "message",
		logID:    payload.MessageID,
	})
}

// ForwardReaction enqueues a reaction payload onto its group's ordered queue.
func (f *HTTPForwarder) ForwardReaction(payload ReactionPayload) {
	body, err := json.Marshal(payload)
	if err != nil {
		f.log.Error("whatsapp forward: drop unmarshalable reaction",
			"group_jid", payload.GroupJID, "target_message_id", payload.TargetMessageID, "err", err)
		return
	}
	f.enqueue(queuedPost{
		groupJID: payload.GroupJID,
		url:      f.reactionURL,
		body:     body,
		logKind:  "reaction",
		logID:    payload.TargetMessageID,
	})
}

// enqueue puts a post onto its group's ordered queue, spawning the group's worker
// on first use. The send blocks only if the group's buffer is full (a sustained
// monolith outage), which is intentional backpressure.
func (f *HTTPForwarder) enqueue(post queuedPost) {
	ch := f.queueFor(post.groupJID)
	select {
	case ch <- post:
	case <-f.ctx.Done():
	}
}

// queueFor returns the group's channel, lazily creating it and its worker.
func (f *HTTPForwarder) queueFor(groupJID string) chan queuedPost {
	f.mu.Lock()
	defer f.mu.Unlock()
	if ch, ok := f.queues[groupJID]; ok {
		return ch
	}
	ch := make(chan queuedPost, forwardQueueDepth)
	f.queues[groupJID] = ch
	go f.worker(ch)
	return ch
}

// worker delivers a single group's posts sequentially, so the group's WhatsApp
// order is preserved end to end.
func (f *HTTPForwarder) worker(ch chan queuedPost) {
	for {
		select {
		case <-f.ctx.Done():
			return
		case post := <-ch:
			f.deliver(post)
		}
	}
}

// deliver POSTs one payload, retrying with capped exponential backoff until the
// monolith accepts it (2xx) or the context is cancelled. It blocks the group's
// worker until then, which is what preserves ordering under a partial outage.
func (f *HTTPForwarder) deliver(post queuedPost) {
	backoff := f.initialBackoff
	for attempt := 1; ; attempt++ {
		err := f.postOnce(post)
		if err == nil {
			return
		}
		f.log.Warn("whatsapp forward failed; will retry",
			"group_jid", post.groupJID,
			"kind", post.logKind,
			"id", post.logID,
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
func (f *HTTPForwarder) postOnce(post queuedPost) error {
	req, err := http.NewRequestWithContext(f.ctx, http.MethodPost, post.url, bytes.NewReader(post.body))
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
