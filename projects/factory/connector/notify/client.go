// Package notify sends bounded, fixed-field notifications to monolith-agent-notify.
// The zero configuration is disabled. Caller keys are deduplicated per UTC day,
// both locally and durably by the monolith's Discord outbox across restarts.
package notify

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	Timeout  = 15 * time.Second
	MaxText  = 1000
	SmokeKey = "factory-notify-live-v1"
)

// Text deliberately has no free-form body, log, error or secret fields. Titles
// and statuses are a fixed vocabulary; links admit only canonical item URLs.
// Key is never sent as text. Unknown JSON fields are discarded by decoding.
type Text struct {
	Key    string
	Title  string
	Link   string
	Status string
	Count  int
}

var itemLink = regexp.MustCompile(`^https://(github\.com/jomcgi-org/homelab/(issues|pull)/[1-9][0-9]{0,9}|factory-plan\.jomcgi\.dev/issue/([1-9][0-9]{0,9}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))$`)

func (t Text) render() (string, error) {
	if len(t.Key) < 1 || len(t.Key) > 256 || t.Count < 0 || t.Count > 9999 {
		return "", errors.New("notify: invalid key or count")
	}
	switch t.Title {
	case "Joe: verify legacy gate explanation", "Factory itsaplan outage", "Factory notify sink is live", "Scheduled agent digest", "Scheduled agent alert":
	default:
		return "", errors.New("notify: title is not in the fixed vocabulary")
	}
	switch t.Status {
	case "", "succeeded", "failed", "blocked", "running":
	default:
		return "", errors.New("notify: status is not in the fixed vocabulary")
	}
	if t.Link != "" && !itemLink.MatchString(t.Link) {
		return "", errors.New("notify: link is not a canonical item URL")
	}
	text := t.Title
	if t.Status != "" {
		text += "\nStatus: " + t.Status
	}
	if t.Count != 0 {
		text += fmt.Sprintf("\nCount: %d", t.Count)
	}
	if t.Link != "" {
		text += "\n" + t.Link
	}
	if len(text) > MaxText {
		return "", errors.New("notify: text exceeds limit")
	}
	return text, nil
}

// Config is supplied only when the chart's connector.notify.enabled is true.
// AppPasswordFile is an Authentik service account app password from 1Password,
// read for every mint so rotation does not require a connector restart.
type Config struct {
	URL             string
	TokenURL        string
	AppPasswordFile string
	// BeforeRequest checks the writer epoch and durably records each attempt.
	BeforeRequest func(context.Context, string, string) error
}

type Client struct {
	config  Config
	http    *http.Client
	lock    chan struct{}
	seen    map[string]bool
	day     string
	now     func() time.Time
	timeout time.Duration
	backoff time.Duration
}

func New(cfg Config) (*Client, error) {
	if cfg.URL == "" && cfg.TokenURL == "" && cfg.AppPasswordFile == "" {
		return &Client{}, nil
	}
	for _, endpoint := range []string{cfg.URL, cfg.TokenURL} {
		u, err := url.Parse(endpoint)
		if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return nil, errors.New("notify: endpoints require HTTPS without userinfo, query or fragment")
		}
	}
	if cfg.AppPasswordFile == "" {
		return nil, errors.New("notify: app password file required")
	}
	if cfg.BeforeRequest == nil {
		return nil, errors.New("notify: writer admission required")
	}
	return &Client{
		config: cfg,
		http:   &http.Client{Timeout: 4 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		lock:   make(chan struct{}, 1), seen: make(map[string]bool), now: time.Now,
		timeout: Timeout, backoff: 250 * time.Millisecond,
	}, nil
}

// Notify is the only send operation. It includes token minting, the MCP
// handshake, bounded retries and dedupe. Errors contain no response bodies,
// caller content, URLs or credentials. A disabled client performs no I/O.
func (c *Client) Notify(ctx context.Context, level string, text Text) error {
	if c.config.URL == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	if level != "info" && level != "warn" && level != "error" {
		return errors.New("notify: invalid level")
	}
	message, err := text.render()
	if err != nil {
		return err
	}
	select {
	case c.lock <- struct{}{}:
		defer func() { <-c.lock }()
	case <-ctx.Done():
		return ctx.Err()
	}
	day := c.now().UTC().Format("2006-01-02")
	if day != c.day {
		c.day, c.seen = day, make(map[string]bool)
	}
	hash := sha256.Sum256([]byte(text.Key))
	key := "factory/" + day + "/" + hex.EncodeToString(hash[:])
	if c.seen[key] {
		return nil
	}
	for attempt := 0; attempt < 3; attempt++ {
		retry, err := c.send(ctx, level, message, key)
		if err == nil {
			// This cache is just an optimization. Server dedupe remains durable
			// when a busy day fills it or a connector process restarts.
			if len(c.seen) < 1024 {
				c.seen[key] = true
			}
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if !retry || attempt == 2 {
			return err
		}
		timer := time.NewTimer(c.backoff * time.Duration(1<<attempt))
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
	return errors.New("notify: attempts exhausted")
}

func (c *Client) send(ctx context.Context, level, message, key string) (bool, error) {
	password, err := os.ReadFile(c.config.AppPasswordFile)
	if err != nil {
		return false, errors.New("notify: cannot read app password")
	}
	if len(bytes.TrimSpace(password)) == 0 {
		return false, errors.New("notify: empty app password")
	}
	form := url.Values{
		"grant_type": {"client_credentials"}, "client_id": {"factory-notify"},
		"username": {"factory-notify-sa"}, "password": {strings.TrimSpace(string(password))},
		"scope": {"openid profile"},
	}
	body, retry, err := c.post(ctx, c.config.TokenURL, "application/x-www-form-urlencoded", "", []byte(form.Encode()), key, "token")
	if err != nil {
		return retry, err
	}
	var token struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if json.Unmarshal(body, &token) != nil || token.AccessToken == "" || token.ExpiresIn <= 0 {
		return false, errors.New("notify: invalid token response")
	}
	_, retry, err = c.rpc(ctx, token.AccessToken, key, "initialize", 1, map[string]any{
		"protocolVersion": "2025-03-26", "capabilities": map[string]any{},
		"clientInfo": map[string]string{"name": "factory-notify", "version": "1"},
	})
	if err != nil {
		return retry, err
	}
	_, retry, err = c.rpc(ctx, token.AccessToken, key, "notifications/initialized", 0, nil)
	if err != nil {
		return retry, err
	}
	result, retry, err := c.rpc(ctx, token.AccessToken, key, "tools/call", 2, map[string]any{
		"name": "monolith_agent_notify", "arguments": map[string]string{
			"message": message, "level": level, "dedupe_key": key,
		},
	})
	if err != nil {
		return retry, err
	}
	var call struct {
		IsError           bool            `json:"isError"`
		StructuredContent json.RawMessage `json:"structuredContent"`
		Content           []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	if json.Unmarshal(result, &call) != nil || call.IsError {
		return false, errors.New("notify: tool refused notification")
	}
	data := call.StructuredContent
	if len(data) == 0 && len(call.Content) == 1 && call.Content[0].Type == "text" {
		data = []byte(call.Content[0].Text)
	}
	var status struct {
		OK     bool  `json:"ok"`
		Queued *bool `json:"queued"`
	}
	if json.Unmarshal(data, &status) != nil || !status.OK || status.Queued == nil {
		return false, errors.New("notify: invalid tool result")
	}
	return false, nil
}

func (c *Client) rpc(ctx context.Context, token, key, method string, id int, params any) (json.RawMessage, bool, error) {
	request := map[string]any{"jsonrpc": "2.0", "method": method}
	if id != 0 {
		request["id"] = id
	}
	if params != nil {
		request["params"] = params
	}
	payload, _ := json.Marshal(request)
	body, retry, err := c.post(ctx, c.config.URL, "application/json", token, payload, key, method)
	if err != nil {
		return nil, retry, err
	}
	if id == 0 {
		return nil, false, nil
	}
	// Streamable HTTP may reply with JSON or an SSE message event. Stateless
	// monolith closes the response after its result; all reads are bounded.
	if !json.Valid(body) {
		scanner := bufio.NewScanner(bytes.NewReader(body))
		for scanner.Scan() {
			if line := scanner.Text(); strings.HasPrefix(line, "data:") {
				candidate := []byte(strings.TrimSpace(strings.TrimPrefix(line, "data:")))
				var event struct {
					ID int `json:"id"`
				}
				if json.Unmarshal(candidate, &event) == nil && event.ID == id {
					body = candidate
					break
				}
			}
		}
	}
	var response struct {
		ID     int             `json:"id"`
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	if json.Unmarshal(body, &response) != nil || response.ID != id || len(response.Error) != 0 || len(response.Result) == 0 {
		return nil, false, errors.New("notify: invalid MCP response")
	}
	return response.Result, false, nil
}

func (c *Client) post(ctx context.Context, endpoint, contentType, bearer string, body []byte, key, phase string) ([]byte, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, false, errors.New("notify: invalid request")
	}
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Accept", "application/json, text/event-stream")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
		req.Header.Set("MCP-Protocol-Version", "2025-03-26")
	}
	if err := c.config.BeforeRequest(ctx, key, phase); err != nil {
		return nil, false, errors.New("notify: writer admission failed")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, true, errors.New("notify: transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		retry := resp.StatusCode == 429 || resp.StatusCode == 500 || resp.StatusCode == 502 || resp.StatusCode == 503 || resp.StatusCode == 504
		return nil, retry, fmt.Errorf("notify: HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	if err != nil {
		return nil, true, errors.New("notify: response read failed")
	}
	if len(data) > 65536 {
		return nil, false, errors.New("notify: response exceeds limit")
	}
	return data, false, nil
}
