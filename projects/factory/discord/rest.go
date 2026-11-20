package discord

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// APIBase is Discord's REST API, version 10.
const APIBase = "https://discord.com/api/v10"

// Discord limits. A Components V2 message holds at most 4,000 characters of
// text across its components.
const (
	flagComponentsV2  = 1 << 15
	componentText     = 10
	maxReplyChars     = 3900
	maxResponseBytes  = 1 << 20
	userAgent         = "DiscordBot (https://github.com/jomcgi-org/homelab, 1)"
	maxRateLimitRetry = 2
)

// REST calls Discord's REST API as the bot.
type REST struct {
	Base  string // APIBase when empty
	Token func() (string, error)
	HTTP  *http.Client
}

// TextDisplay is a Components V2 text display.
type TextDisplay struct {
	Type    int    `json:"type"`
	Content string `json:"content"`
}

// AllowedMentions is Discord's mention filter. An empty Parse list with no
// users or roles lets the message ping nobody (HOME-162 contract rule 6).
type AllowedMentions struct {
	Parse       []string `json:"parse"`
	RepliedUser bool     `json:"replied_user"`
}

// MessageReference makes a message a reply.
type MessageReference struct {
	MessageID       string `json:"message_id"`
	FailIfNotExists bool   `json:"fail_if_not_exists"`
}

// Reply is the body the bridge posts. Components V2 messages carry their
// text in components; content must stay empty.
type Reply struct {
	Nonce            string            `json:"nonce,omitempty"`
	EnforceNonce     bool              `json:"enforce_nonce,omitempty"`
	Flags            int               `json:"flags"`
	Components       []TextDisplay     `json:"components"`
	AllowedMentions  AllowedMentions   `json:"allowed_mentions"`
	MessageReference *MessageReference `json:"message_reference,omitempty"`
}

// NewReply builds a reply to message id that cannot ping anyone, clipped to
// Discord's limit.
func NewReply(text, replyTo string) Reply {
	text = strings.TrimSpace(text)
	if text == "" {
		text = "(no answer)"
	}
	if utf8.RuneCountInString(text) > maxReplyChars {
		runes := []rune(text)
		text = string(runes[:maxReplyChars-1]) + "…"
	}
	r := Reply{
		Flags:           flagComponentsV2,
		Components:      []TextDisplay{{Type: componentText, Content: text}},
		AllowedMentions: AllowedMentions{Parse: []string{}},
	}
	if replyTo != "" {
		r.MessageReference = &MessageReference{MessageID: replyTo}
	}
	return r
}

// Send posts a reply in a channel.
func (r *REST) Send(ctx context.Context, channelID string, reply Reply) error {
	return r.do(ctx, http.MethodPost, "/channels/"+channelID+"/messages", reply, nil)
}

// Me is the bot's own user.
func (r *REST) Me(ctx context.Context) (User, error) {
	var u User
	err := r.do(ctx, http.MethodGet, "/users/@me", nil, &u)
	return u, err
}

// GatewayURL is the bot's gateway address.
func (r *REST) GatewayURL(ctx context.Context) (string, error) {
	var out struct {
		URL string `json:"url"`
	}
	if err := r.do(ctx, http.MethodGet, "/gateway/bot", nil, &out); err != nil {
		return "", err
	}
	if out.URL == "" {
		return "", errors.New("discord: gateway URL is empty")
	}
	return out.URL, nil
}

// HTTPError is a non-2xx Discord answer. Body is clipped and holds no token.
type HTTPError struct {
	Status int
	Body   string
}

func (e *HTTPError) Error() string { return fmt.Sprintf("discord: HTTP %d: %s", e.Status, e.Body) }

func (r *REST) do(ctx context.Context, method, path string, in, out any) error {
	base := r.Base
	if base == "" {
		base = APIBase
	}
	client := r.HTTP
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	var body []byte
	if in != nil {
		var err error
		if body, err = json.Marshal(in); err != nil {
			return err
		}
	}
	for attempt := 0; ; attempt++ {
		token, err := r.Token()
		if err != nil {
			return fmt.Errorf("discord: bot token: %w", err)
		}
		req, err := http.NewRequestWithContext(ctx, method, base+path, bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bot "+token)
		req.Header.Set("User-Agent", userAgent)
		if in != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
		resp.Body.Close()
		if err != nil {
			return err
		}
		if resp.StatusCode == http.StatusTooManyRequests && attempt < maxRateLimitRetry {
			if err := sleep(ctx, retryAfter(resp.Header, data)); err != nil {
				return err
			}
			continue
		}
		if resp.StatusCode < 200 || resp.StatusCode > 299 {
			return &HTTPError{Status: resp.StatusCode, Body: clip(string(data), 300)}
		}
		if out == nil {
			return nil
		}
		return json.Unmarshal(data, out)
	}
}

// retryAfter reads Discord's rate-limit wait, bounded to a minute.
func retryAfter(h http.Header, body []byte) time.Duration {
	seconds, err := strconv.ParseFloat(h.Get("Retry-After"), 64)
	if err != nil {
		var b struct {
			RetryAfter float64 `json:"retry_after"`
		}
		if json.Unmarshal(body, &b) == nil {
			seconds = b.RetryAfter
		}
	}
	return min(max(time.Duration(seconds*float64(time.Second)), 100*time.Millisecond), time.Minute)
}

func sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
