package itsaplan

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// DecisionHandler accepts POST /decide with {"key":"HOME-42","choice":1}
// with the displayed choice revision in If-Match, after the listener has authenticated the caller.
type DecisionHandler struct {
	WithFence func(context.Context, int64, func() error) (bool, error)
	Plan      *Client
	Projects  []string
	Password  func() (string, error)
	Origin    string
	mu        sync.Mutex
}

func (h *DecisionHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var pick struct {
		Key    string
		Choice int
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&pick) != nil || decoder.Decode(new(any)) != io.EOF || pick.Choice < 1 || pick.Choice > 4 {
		http.Error(w, "invalid choice", 400)
		return
	}
	revision := r.Header.Get("If-Match")
	if revision == "" {
		http.Error(w, "choice revision required", http.StatusPreconditionRequired)
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	for _, project := range h.Projects {
		if !strings.HasPrefix(pick.Key, project+"-") {
			continue
		}
		number, err := strconv.ParseInt(strings.TrimPrefix(pick.Key, project+"-"), 10, 64)
		if err != nil || number < 1 || pick.Key != project+"-"+strconv.FormatInt(number, 10) {
			break
		}
		err = h.record(ctx, project, number, pick.Choice, revision)
		switch {
		case errors.Is(err, ErrNotFound):
			http.Error(w, "issue unavailable", 404)
		case errors.Is(err, ErrConflict):
			http.Error(w, "handoff unavailable", 409)
		case err != nil:
			http.Error(w, "decision write failed", 502)
		default:
			w.WriteHeader(http.StatusNoContent)
		}
		return
	}
	http.Error(w, "issue unavailable", 404)
}

func (h *DecisionHandler) record(ctx context.Context, project string, number int64, choice int, revision string) error {
	p, err := h.Plan.GetProject(ctx, project)
	if err != nil {
		return err
	}
	issue, err := h.Plan.GetIssueByNumber(ctx, project, number)
	if err != nil {
		return err
	}
	owner := p.memberID("joe")
	if owner == "" || issue.AssigneeUserID != owner || issue.ArchivedAt != nil || issue.DelegateUserID != nil || !p.openColumn(issue.ColumnID) {
		return ErrConflict
	}
	choices := parseChoices(issue.Description)
	if choice > len(choices) || strconv.Quote(choiceRevision(choices)) != revision {
		return ErrConflict
	}
	decision := "Decision: " + choices[choice-1]
	id := issue.ID
	user := p.memberID("joesessions")
	agents := map[string]bool{}
	for _, a := range p.Assignees {
		if a.Kind == "agent" {
			agents[a.UserID] = true
		}
	}
	if user == "" {
		return ErrConflict
	}
	// itsaplan writes take no revision precondition. Read the issue again
	// inside the fence just before each write, so an edit or reorder made
	// after the first read still returns 409 instead of a decision.
	verify := func() error {
		current, err := h.Plan.GetIssueByNumber(ctx, project, number)
		if err != nil {
			return err
		}
		if current.ID != id || current.AssigneeUserID != owner || current.ArchivedAt != nil || current.DelegateUserID != nil || !p.openColumn(current.ColumnID) {
			return ErrConflict
		}
		if strconv.Quote(choiceRevision(parseChoices(current.Description))) != revision {
			return ErrConflict
		}
		return nil
	}
	path := "/issues/" + strconv.FormatInt(id, 10)
	q := url.Values{"order": {"desc"}, "limit": {"100"}}
	// The feed is newest first. Only the latest decision comment counts: a
	// comment for a choice list edited after it was saved does not block a
	// fresh pick from the current list, which supersedes it.
	recorded, latest := false, true
	for {
		var page feedPage
		if err := h.Plan.do(ctx, h.Plan.Coordinator, http.MethodGet, path+"/feed?"+q.Encode(), nil, &page); err != nil {
			return err
		}
		for _, item := range page.Items {
			if item.IssueID != id {
				return ErrConflict
			}
			if item.Kind == "comment" && item.ActorUserID == user && strings.HasPrefix(item.Body, "Decision: ") {
				if !item.OriginalComment() {
					return ErrConflict
				}
				if latest {
					recorded = item.Body == decision
					latest = false
				}
			}
			if item.Kind == "activity" && item.Action == "delegate" {
				var previous string
				if item.Payload.From == nil || json.Unmarshal(item.Payload.From.ID, &previous) != nil || !agents[previous] || item.Payload.To != nil {
					return ErrConflict
				}
				admitted, err := h.WithFence(ctx, id, func() error { return h.write(ctx, path, decision, previous, user, recorded, verify) })
				if err == nil && !admitted {
					return ErrConflict
				}
				return err
			}
		}
		if page.NextCursor == nil {
			return ErrConflict
		}
		cursor, err := json.Marshal(page.NextCursor)
		if err != nil {
			return err
		}
		q.Set("cursor", string(cursor))
	}
}

func (h *DecisionHandler) write(ctx context.Context, path, decision, delegate, expectedUser string, recorded bool, verify func() error) error {
	password, err := h.Password()
	if err != nil {
		return err
	}
	var cookies []*http.Cookie
	call := func(method, path string, body map[string]string) error {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		req, err := http.NewRequestWithContext(ctx, method, h.Plan.BaseURL+path, bytes.NewReader(data))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Origin", h.Origin)
		for _, cookie := range cookies {
			req.AddCookie(cookie)
		}
		resp, err := h.Plan.HTTP.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return errors.New("session request failed")
		}
		if cookies == nil {
			// Bind the login to the project member resolved for this handoff.
			var login struct {
				User struct {
					ID string `json:"id"`
				} `json:"user"`
			}
			if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&login); err != nil || login.User.ID != expectedUser {
				return errors.New("unexpected decision author")
			}

			cookies = resp.Cookies()
			if len(cookies) == 0 {
				return errors.New("missing session cookie")
			}
		}
		return nil
	}
	if err := call("POST", "/api/auth/sign-in/email", map[string]string{"email": "joe-sessions@jomcgi.dev", "password": password}); err != nil {
		return err
	}
	defer call("POST", "/api/auth/sign-out", map[string]string{})
	if !recorded {
		if err := verify(); err != nil {
			return err
		}
		if err := call("POST", path+"/comments", map[string]string{"body": decision}); err != nil {
			return err
		}
	}
	if err := verify(); err != nil {
		return err
	}
	return call("PATCH", path, map[string]string{"delegateUserId": delegate})
}
