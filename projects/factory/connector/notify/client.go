// Package notify sends fixed-field notifications to the factory Discord bridge.
package notify

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
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

func (t Text) Render() (string, error) {
	if len(t.Key) < 1 || len(t.Key) > 256 || t.Count < 0 || t.Count > 9999 {
		return "", errors.New("notify: invalid key or count")
	}
	switch t.Title {
	case "Factory itsaplan outage", "Factory notify sink is live", "Scheduled agent digest", "Scheduled agent alert":
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
// AppPasswordFile contains the connector’s bridge credential,
// read for every request so rotation does not require a connector restart.
type Config struct {
	URL             string
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
	if cfg.URL == "" && cfg.AppPasswordFile == "" {
		return &Client{}, nil
	}
	u, err := url.Parse(cfg.URL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("notify: invalid bridge URL")
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

// Notify is the only send operation. It bounds retries and deduplicates successful sends per UTC day. Errors contain no response bodies,
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
	_, err := text.Render()
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
		retry, err := c.send(ctx, level, text, key)
		if err == nil {
			// Discord also deduplicates recent retries by nonce.
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

type Request struct {
	Channel string
	Level   string
	Text    Text
}

func (c *Client) send(ctx context.Context, level string, text Text, key string) (bool, error) {
	password, err := os.ReadFile(c.config.AppPasswordFile)
	if err != nil || len(bytes.TrimSpace(password)) == 0 {
		return false, errors.New("notify: missing caller credential")
	}
	text.Key = key
	body, err := json.Marshal(Request{Channel: "homelab", Level: level, Text: text})
	if err != nil {
		return false, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.config.URL, bytes.NewReader(body))
	if err != nil {
		return false, errors.New("notify: invalid request")
	}
	req.SetBasicAuth("factory-connector", strings.TrimSpace(string(password)))
	req.Header.Set("Content-Type", "application/json")
	if err := c.config.BeforeRequest(ctx, key, "tools/call"); err != nil {
		return false, errors.New("notify: writer admission failed")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return true, errors.New("notify: transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return resp.StatusCode == 429 || resp.StatusCode >= 500, fmt.Errorf("notify: HTTP %d", resp.StatusCode)
	}
	return false, nil
}
