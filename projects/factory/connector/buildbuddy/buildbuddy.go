// Package buildbuddy reads which targets and tests failed in a BuildBuddy
// invocation, so a revision round can name them. It returns identifiers
// only: Bazel labels, pytest node IDs without parameters, Go test names and
// JavaScript test files, each matched against a fixed character set. No other
// log text leaves this package, because the log carries the output of the
// code under test.
package buildbuddy

import (
	"bytes"
	"context"
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

// Bounds on one invocation's log and on what is reported from it.
const (
	MaxPages     = 64
	MaxPageBytes = 4 << 20
	MaxTargets   = 20
	MaxTests     = 30
	maxIDLength  = 200
)

// Failures names what failed in one invocation.
type Failures struct {
	Targets []string
	Tests   []string
	// Truncated is set when the log or the lists hit a bound.
	Truncated bool
}

// Empty reports no failing target or test.
func (f *Failures) Empty() bool { return len(f.Targets) == 0 && len(f.Tests) == 0 }

// Client calls BuildBuddy's GetLog API with a read-only API key.
type Client struct {
	// APIURL is the organisation's base URL, for example
	// https://example.buildbuddy.io.
	APIURL string
	// InvocationPrefix is the URL a status links to before the invocation ID,
	// for example https://example.buildbuddy.io/invocation/.
	InvocationPrefix string
	// KeyFile holds the API key; it is read per call so a rotation needs no
	// restart.
	KeyFile string
	HTTP    *http.Client
}

var invocationID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// Invocation returns the invocation ID a status URL links to, only when the
// URL is exactly the configured prefix followed by an invocation UUID. The
// API call is then built from the configured URL, never from the status.
func (c *Client) Invocation(statusURL string) (string, bool) {
	if c == nil || c.InvocationPrefix == "" || !strings.HasPrefix(statusURL, c.InvocationPrefix) {
		return "", false
	}
	id := strings.TrimPrefix(statusURL, c.InvocationPrefix)
	if !invocationID.MatchString(id) {
		return "", false
	}
	return id, true
}

// Error is a failed read, by fixed class.
type Error struct {
	Class string // "status_<code>", "transport", "decode", "key", "too_large"
	Err   error
}

func (e *Error) Error() string { return "buildbuddy " + e.Class + ": " + e.Err.Error() }
func (e *Error) Unwrap() error { return e.Err }

// Class returns err's fixed class, or "error".
func Class(err error) string {
	var be *Error
	if errors.As(err, &be) {
		return be.Class
	}
	return "error"
}

type getLogRequest struct {
	Selector struct {
		InvocationID string `json:"invocation_id"`
	} `json:"selector"`
	PageToken string `json:"page_token,omitempty"`
}

type getLogResponse struct {
	Log struct {
		Contents string `json:"contents"`
	} `json:"log"`
	NextPageToken string `json:"nextPageToken"`
}

// Failures reads the invocation's build log page by page and parses each
// page as it arrives; the log itself is never kept.
func (c *Client) Failures(ctx context.Context, id string) (*Failures, error) {
	if !invocationID.MatchString(id) {
		return nil, &Error{Class: "decode", Err: fmt.Errorf("invocation ID %q", id)}
	}
	key, err := os.ReadFile(c.KeyFile)
	if err != nil || len(bytes.TrimSpace(key)) == 0 {
		if err == nil {
			err = errors.New("empty API key")
		}
		return nil, &Error{Class: "key", Err: err}
	}
	endpoint, err := url.JoinPath(c.APIURL, "/api/v1/GetLog")
	if err != nil {
		return nil, &Error{Class: "decode", Err: err}
	}
	client := c.HTTP
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	p := newParser()
	token := ""
	for page := 0; page < MaxPages; page++ {
		var req getLogRequest
		req.Selector.InvocationID = id
		req.PageToken = token
		body, _ := json.Marshal(req)
		hr, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, &Error{Class: "transport", Err: err}
		}
		hr.Header.Set("Content-Type", "application/json")
		hr.Header.Set("x-buildbuddy-api-key", string(bytes.TrimSpace(key)))
		resp, err := client.Do(hr)
		if err != nil {
			return nil, &Error{Class: "transport", Err: err}
		}
		raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxPageBytes+1))
		resp.Body.Close()
		switch {
		case err != nil:
			return nil, &Error{Class: "transport", Err: err}
		case resp.StatusCode != http.StatusOK:
			return nil, &Error{Class: fmt.Sprintf("status_%d", resp.StatusCode), Err: fmt.Errorf("GetLog page %d", page)}
		case len(raw) > MaxPageBytes:
			return nil, &Error{Class: "too_large", Err: fmt.Errorf("GetLog page %d exceeds %d bytes", page, MaxPageBytes)}
		}
		var out getLogResponse
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, &Error{Class: "decode", Err: err}
		}
		p.write(out.Log.Contents)
		if out.NextPageToken == "" || out.NextPageToken == token {
			return p.done(false), nil
		}
		token = out.NextPageToken
	}
	return p.done(true), nil
}

// Parse returns the failures named in a whole log.
func Parse(log string) *Failures {
	p := newParser()
	p.write(log)
	return p.done(false)
}

var (
	ansi = regexp.MustCompile(`\x1b\[[0-9;]*[A-Za-z]`)
	// A Bazel label: //package:name, no external repositories.
	label = `//[A-Za-z0-9_\-./+]*:[A-Za-z0-9_\-./+=,@~]+`
	// Bazel's test summary ("//pkg:test   FAILED in 2 out of 2 ..."), its
	// "FAILED: //pkg:test (Summary)" line, and an action that failed while
	// building a target ("ERROR: ...: GoCompilePkg //pkg:lib failed: ...").
	summaryLine = regexp.MustCompile(`^(` + label + `)\s+(?:\(cached\)\s+)?(?:FAILED|TIMEOUT|NO STATUS|INCOMPLETE)\b`)
	failedLine  = regexp.MustCompile(`^FAILED: (` + label + `) `)
	buildError  = regexp.MustCompile(`^ERROR: .* (` + label + `) failed`)
	// pytest's short summary, without the parameter ID: "FAILED path.py::test".
	pytestLine = regexp.MustCompile(`^FAILED ([A-Za-z0-9_\-./]+\.py::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?)`)
	// go test, top-level test names only.
	goLine = regexp.MustCompile(`^\s*--- FAIL: (Test[A-Za-z0-9_]*)`)
	// vitest and jest name the failing file.
	jsLine = regexp.MustCompile(`^\s*(?:FAIL|×)\s+([A-Za-z0-9_\-./]+\.(?:test|spec)\.[cm]?[jt]sx?)\b`)
)

type parser struct {
	partial   string
	targets   []string
	tests     []string
	seen      map[string]bool
	truncated bool
}

func newParser() *parser { return &parser{seen: map[string]bool{}} }

// maxLine bounds a line carried across pages; no line worth parsing is
// longer, so an unterminated one is dropped rather than kept growing.
const maxLine = 64 << 10

// write feeds the next chunk; a line split across pages is joined.
func (p *parser) write(chunk string) {
	text := p.partial + chunk
	lines := strings.Split(text, "\n")
	p.partial = lines[len(lines)-1]
	if len(p.partial) > maxLine {
		p.partial = ""
	}
	for _, l := range lines[:len(lines)-1] {
		p.line(l)
	}
}

func (p *parser) line(l string) {
	l = strings.TrimRight(ansi.ReplaceAllString(l, ""), "\r")
	for _, re := range []*regexp.Regexp{summaryLine, failedLine, buildError} {
		if m := re.FindStringSubmatch(l); m != nil {
			p.add(&p.targets, MaxTargets, m[1])
			return
		}
	}
	for _, re := range []*regexp.Regexp{pytestLine, goLine, jsLine} {
		if m := re.FindStringSubmatch(l); m != nil {
			p.add(&p.tests, MaxTests, m[1])
			return
		}
	}
}

func (p *parser) add(list *[]string, max int, id string) {
	if len(id) > maxIDLength || p.seen[id] {
		return
	}
	if len(*list) >= max {
		p.truncated = true
		return
	}
	p.seen[id] = true
	*list = append(*list, id)
}

func (p *parser) done(truncated bool) *Failures {
	if p.partial != "" {
		p.line(p.partial)
		p.partial = ""
	}
	return &Failures{Targets: p.targets, Tests: p.tests, Truncated: truncated || p.truncated}
}
