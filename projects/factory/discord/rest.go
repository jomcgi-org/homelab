package discord

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
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
	componentGallery  = 12
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

// ReplyComponent is a Components V2 text display, media gallery,
// or separator (layout.go).
type ReplyComponent struct {
	Type    int         `json:"type"`
	Content string      `json:"content,omitempty"`
	Items   []MediaItem `json:"items,omitempty"`
	Divider *bool       `json:"divider,omitempty"`
	Spacing int         `json:"spacing,omitempty"`
}

// MediaItem is one media gallery item; URL names an uploaded file as
// attachment://<filename>.
type MediaItem struct {
	Media struct {
		URL string `json:"url"`
	} `json:"media"`
}

// ReplyAttachment declares an uploaded file in a message body.
type ReplyAttachment struct {
	ID       int    `json:"id"`
	Filename string `json:"filename"`
}

// File is a PNG the bridge uploads with a reply.
type File struct {
	Name string
	Data []byte
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
// text in components; content must stay empty. A poll message (NewPollReply)
// has no flags and no components.
type Reply struct {
	Nonce            string            `json:"nonce,omitempty"`
	EnforceNonce     bool              `json:"enforce_nonce,omitempty"`
	Flags            int               `json:"flags"`
	Components       []ReplyComponent  `json:"components,omitempty"`
	Poll             *Poll             `json:"poll,omitempty"`
	AllowedMentions  AllowedMentions   `json:"allowed_mentions"`
	MessageReference *MessageReference `json:"message_reference,omitempty"`
	Attachments      []ReplyAttachment `json:"attachments,omitempty"`
	// Files are uploaded with the message, in Attachments order.
	Files []File `json:"-"`
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
		Components:      []ReplyComponent{{Type: componentText, Content: text}},
		AllowedMentions: AllowedMentions{Parse: []string{}},
	}
	if replyTo != "" {
		r.MessageReference = &MessageReference{MessageID: replyTo}
	}
	return r
}

// Send posts a reply in a channel.
func (r *REST) Send(ctx context.Context, channelID string, reply Reply) error {
	_, err := r.Post(ctx, channelID, reply)
	return err
}

// Post posts a reply in a channel, with rule lines as dividers (Layout),
// and returns the new message's ID.
func (r *REST) Post(ctx context.Context, channelID string, reply Reply) (string, error) {
	reply = reply.Layout()
	path := "/channels/" + channelID + "/messages"
	var out struct {
		ID string `json:"id"`
	}
	if len(reply.Files) == 0 {
		err := r.do(ctx, http.MethodPost, path, reply, &out)
		return out.ID, err
	}
	payload, err := json.Marshal(reply)
	if err != nil {
		return "", err
	}
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	if err := w.WriteField("payload_json", string(payload)); err != nil {
		return "", err
	}
	for n, f := range reply.Files {
		h := textproto.MIMEHeader{}
		h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="files[%d]"; filename=%q`, n, f.Name))
		h.Set("Content-Type", "image/png")
		part, err := w.CreatePart(h)
		if err != nil {
			return "", err
		}
		if _, err := part.Write(f.Data); err != nil {
			return "", err
		}
	}
	if err := w.Close(); err != nil {
		return "", err
	}
	err = r.send(ctx, http.MethodPost, path, body.Bytes(), w.FormDataContentType(), &out)
	return out.ID, err
}

// React adds a Unicode emoji reaction as the bot. It needs the Add
// Reactions and Read Message History permissions.
func (r *REST) React(ctx context.Context, channelID, messageID, emoji string) error {
	return r.do(ctx, http.MethodPut, "/channels/"+channelID+"/messages/"+messageID+"/reactions/"+url.PathEscape(emoji)+"/@me", nil, nil)
}

// threadArchiveMinutes hides an idle thread after a day.
const threadArchiveMinutes = 1440

// StartThread starts a public thread from a message and returns its ID. It
// needs the Create Public Threads permission.
func (r *REST) StartThread(ctx context.Context, channelID, messageID, name string) (string, error) {
	in := struct {
		Name                string `json:"name"`
		AutoArchiveDuration int    `json:"auto_archive_duration"`
	}{name, threadArchiveMinutes}
	var out struct {
		ID string `json:"id"`
	}
	if err := r.do(ctx, http.MethodPost, "/channels/"+channelID+"/messages/"+messageID+"/threads", in, &out); err != nil {
		return "", err
	}
	if out.ID == "" {
		return "", errors.New("discord: thread ID is empty")
	}
	return out.ID, nil
}

// Pin pins a message. It needs the Pin Messages permission (formerly part
// of Manage Messages).
func (r *REST) Pin(ctx context.Context, channelID, messageID string) error {
	return r.do(ctx, http.MethodPut, "/channels/"+channelID+"/messages/pins/"+messageID, nil, nil)
}

// Typing shows the bot as typing in a channel for 10 seconds or until it
// posts.
func (r *REST) Typing(ctx context.Context, channelID string) error {
	return r.do(ctx, http.MethodPost, "/channels/"+channelID+"/typing", nil, nil)
}

// Me is the bot's own user.
func (r *REST) Me(ctx context.Context) (User, error) {
	var u User
	err := r.do(ctx, http.MethodGet, "/users/@me", nil, &u)
	return u, err
}

// ClearCommands removes every global slash command the application has
// registered. The hub bot that used this token registered /agent and others;
// the bridge registers none, and nothing else re-syncs them, so they stay in
// Discord's picker and answer "application did not respond" until cleared.
func (r *REST) ClearCommands(ctx context.Context) error {
	var app struct {
		ID string `json:"id"`
	}
	if err := r.do(ctx, http.MethodGet, "/oauth2/applications/@me", nil, &app); err != nil {
		return err
	}
	if app.ID == "" {
		return errors.New("discord: application ID is empty")
	}
	return r.do(ctx, http.MethodPut, "/applications/"+app.ID+"/commands", []struct{}{}, nil)
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
	var body []byte
	contentType := ""
	if in != nil {
		var err error
		if body, err = json.Marshal(in); err != nil {
			return err
		}
		contentType = "application/json"
	}
	return r.send(ctx, method, path, body, contentType, out)
}

// send makes one call with a ready body, retrying a rate limit.
func (r *REST) send(ctx context.Context, method, path string, body []byte, contentType string, out any) error {
	base := r.Base
	if base == "" {
		base = APIBase
	}
	client := r.HTTP
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
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
		if contentType != "" {
			req.Header.Set("Content-Type", contentType)
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
		if out == nil || len(bytes.TrimSpace(data)) == 0 {
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
