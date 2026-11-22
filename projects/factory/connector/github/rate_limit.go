package github

import (
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// RateLimit is shared by the connector clients that use one GitHub App installation.
type RateLimit struct {
	Log *slog.Logger

	mu                       sync.Mutex
	rest, graphql, secondary time.Time
}

func (r *RateLimit) check(path string) error {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	until := r.rest
	if path == "/graphql" {
		until = r.graphql
	}
	if r.secondary.After(until) {
		until = r.secondary
	}
	if time.Now().Before(until) {
		return fmt.Errorf("github: installation rate limited until %s", until.UTC().Format(time.RFC3339))
	}
	return nil
}

func (r *RateLimit) observe(req *http.Request, resp *http.Response, body any, data []byte) {
	if r == nil {
		return
	}
	h := resp.Header
	now := time.Now()
	var reset, retry time.Time
	if h.Get("X-RateLimit-Remaining") == "0" {
		if n, err := strconv.ParseInt(h.Get("X-RateLimit-Reset"), 10, 64); err == nil {
			reset = time.Unix(n, 0)
		}
		if !reset.After(now) {
			reset = now.Add(time.Minute)
		}
	}
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusTooManyRequests {
		delay := h.Get("Retry-After")
		if n, err := strconv.Atoi(delay); err == nil && n >= 0 && n <= 86400 {
			retry = now.Add(time.Duration(n) * time.Second)
		} else if t, err := http.ParseTime(delay); err == nil {
			retry = t
		}
		if !reset.After(now) && !retry.After(now) &&
			(resp.StatusCode == http.StatusTooManyRequests || strings.Contains(strings.ToLower(string(data)), "rate limit")) {
			retry = now.Add(time.Minute)
		}
	}
	path := req.URL.EscapedPath()
	r.mu.Lock()
	bucket := &r.rest
	if path == "/graphql" {
		bucket = &r.graphql
	}
	if reset.After(*bucket) {
		*bucket = reset
	}
	if retry.After(r.secondary) {
		r.secondary = retry
	}
	r.mu.Unlock()

	if r.Log != nil {
		r.Log.Info("github API request", "operation", githubOperation(path, body),
			"method", req.Method, "status", resp.StatusCode,
			"resource", h.Get("X-RateLimit-Resource"), "used", h.Get("X-RateLimit-Used"),
			"remaining", h.Get("X-RateLimit-Remaining"), "reset", h.Get("X-RateLimit-Reset"))
	}
}

// githubOperation omits repository names, object IDs, branch names and query variables.
func githubOperation(path string, body any) string {
	if path == "/graphql" {
		request, _ := body.(map[string]any)
		query, _ := request["query"].(string)
		_, name, _ := strings.Cut(query, " ")
		if name, _, ok := strings.Cut(name, "("); ok && name != "" {
			return "graphql/" + name
		}
		return "graphql"
	}
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	if len(parts) < 4 || parts[0] != "repos" {
		return "other"
	}
	switch parts[3] {
	case "pulls", "commits", "issues", "check-runs", "compare", "git", "activity", "branches":
		if len(parts) >= 6 {
			switch parts[5] {
			case "check-runs", "status", "statuses", "pulls", "commits", "comments", "heads":
				return parts[3] + "/" + parts[5]
			}
		}
		return parts[3]
	default:
		return "other"
	}
}
