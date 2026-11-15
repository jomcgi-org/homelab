package github

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// MaxCommentBody is the largest comment body the proxy writes, in bytes.
// GitHub takes 65536 characters.
const MaxCommentBody = 65536

// maxCommentPage bounds the page a listing may ask for: 100 pages of 100.
const maxCommentPage = 100

// CommentProxy writes the reviewer's PR conversation comments with the
// reviewer App's comment token, which GitHub requires to hold
// pull_requests write and which could therefore approve a PR. The token
// never leaves the broker. The proxy does three things with it: list a PR's
// conversation comments, create one on a pull request, and edit one that
// Author wrote. Callers wrap it with TokenReview authorization.
type CommentProxy struct {
	// Minter mints the comment token for a bound repository.
	Minter *Minter
	// Author is the reviewer App's bot login.
	Author string
	Logger *slog.Logger
}

// ProxiedComment is a conversation comment as the proxy returns it.
type ProxiedComment struct {
	ID     int64  `json:"id"`
	Author string `json:"author"`
	Body   string `json:"body"`
}

type ghComment struct {
	ID      int64  `json:"id"`
	Body    string `json:"body"`
	HTMLURL string `json:"html_url"`
	User    struct {
		Login string `json:"login"`
	} `json:"user"`
}

func (c ghComment) proxied() ProxiedComment {
	return ProxiedComment{ID: c.ID, Author: c.User.Login, Body: c.Body}
}

var (
	errGitHubNotFound = errors.New("not found on GitHub")
	errNotAPR         = errors.New("not a pull request")
	errNotOwn         = errors.New("not the reviewer's comment")
)

func (p *CommentProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	switch r.Method {
	case http.MethodGet:
		p.list(w, r)
	case http.MethodPost:
		p.create(w, r)
	case http.MethodPatch:
		p.update(w, r)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

// list serves GET ?repository=owner/name&number=N&page=P: one page of up to
// 100 comments, oldest first, and whether another page follows.
func (p *CommentProxy) list(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	repo := q.Get("repository")
	number, err1 := strconv.Atoi(q.Get("number"))
	page, err2 := strconv.Atoi(q.Get("page"))
	if err1 != nil || err2 != nil || number <= 0 || page <= 0 || page > maxCommentPage {
		p.fail(w, "list", repo, http.StatusBadRequest, "bad_request", nil)
		return
	}
	var out []ghComment
	v := url.Values{"per_page": {"100"}, "page": {strconv.Itoa(page)}}
	next, err := p.call(r.Context(), repo, http.MethodGet, "/issues/"+strconv.Itoa(number)+"/comments?"+v.Encode(), nil, &out)
	if err != nil {
		p.failCall(w, "list", repo, err)
		return
	}
	comments := make([]ProxiedComment, 0, len(out))
	for _, c := range out {
		comments = append(comments, c.proxied())
	}
	writeJSON(w, http.StatusOK, map[string]any{"comments": comments, "next": next})
}

// create serves POST {repository, number, body}: a comment on a pull
// request. An issue number is refused.
func (p *CommentProxy) create(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Repository string `json:"repository"`
		Number     int    `json:"number"`
		Body       string `json:"body"`
	}
	if !p.decode(w, r, "create", &in) {
		return
	}
	if in.Number <= 0 || !validBody(in.Body) {
		p.fail(w, "create", in.Repository, http.StatusBadRequest, "bad_request", nil)
		return
	}
	ctx := r.Context()
	var pr struct {
		Number int `json:"number"`
	}
	if _, err := p.call(ctx, in.Repository, http.MethodGet, "/pulls/"+strconv.Itoa(in.Number), nil, &pr); err != nil {
		if errors.Is(err, errGitHubNotFound) {
			err = errNotAPR
		}
		p.failCall(w, "create", in.Repository, err)
		return
	}
	var out ghComment
	if _, err := p.call(ctx, in.Repository, http.MethodPost, "/issues/"+strconv.Itoa(in.Number)+"/comments",
		map[string]string{"body": in.Body}, &out); err != nil {
		p.failCall(w, "create", in.Repository, err)
		return
	}
	p.logger().InfoContext(ctx, "reviewer comment created", "repository", in.Repository, "pr", in.Number, "comment", out.ID)
	writeJSON(w, http.StatusCreated, out.proxied())
}

// update serves PATCH {repository, id, body}: a new body for a comment on a
// pull request that Author wrote. Any other comment is refused.
func (p *CommentProxy) update(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Repository string `json:"repository"`
		ID         int64  `json:"id"`
		Body       string `json:"body"`
	}
	if !p.decode(w, r, "update", &in) {
		return
	}
	if in.ID <= 0 || !validBody(in.Body) {
		p.fail(w, "update", in.Repository, http.StatusBadRequest, "bad_request", nil)
		return
	}
	ctx := r.Context()
	path := "/issues/comments/" + strconv.FormatInt(in.ID, 10)
	var cur ghComment
	if _, err := p.call(ctx, in.Repository, http.MethodGet, path, nil, &cur); err != nil {
		p.failCall(w, "update", in.Repository, err)
		return
	}
	if !strings.EqualFold(cur.User.Login, p.Author) || !strings.Contains(cur.HTMLURL, "/pull/") {
		p.failCall(w, "update", in.Repository, errNotOwn)
		return
	}
	var out ghComment
	if _, err := p.call(ctx, in.Repository, http.MethodPatch, path, map[string]string{"body": in.Body}, &out); err != nil {
		p.failCall(w, "update", in.Repository, err)
		return
	}
	writeJSON(w, http.StatusOK, out.proxied())
}

func validBody(b string) bool {
	return strings.TrimSpace(b) != "" && len(b) <= MaxCommentBody
}

func (p *CommentProxy) decode(w http.ResponseWriter, r *http.Request, op string, v any) bool {
	dec := json.NewDecoder(io.LimitReader(r.Body, MaxCommentBody+4096))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		p.fail(w, op, "", http.StatusBadRequest, "bad_request", err)
		return false
	}
	return true
}

// call sends one REST request for a bound repository with the comment
// token. next reports a Link rel="next" on the answer.
func (p *CommentProxy) call(ctx context.Context, repo, method, path string, body, out any) (next bool, err error) {
	tok, err := p.Minter.ConnectorToken(ctx, repo)
	if err != nil {
		return false, err
	}
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return false, err
		}
		rd = bytes.NewReader(data)
	}
	a := p.Minter.App
	u := strings.TrimRight(a.APIURL, "/") + "/repos/" + repo + path
	req, err := http.NewRequestWithContext(ctx, method, u, rd)
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", "Bearer "+tok.Token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.client().Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return false, err
	}
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return false, errGitHubNotFound
	case resp.StatusCode < 200 || resp.StatusCode > 299:
		var msg struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(data, &msg)
		return false, fmt.Errorf("GitHub %s %s: HTTP %d: %s", method, strings.SplitN(path, "?", 2)[0], resp.StatusCode, msg.Message)
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return false, fmt.Errorf("decode GitHub answer: %w", err)
		}
	}
	return strings.Contains(resp.Header.Get("Link"), `rel="next"`), nil
}

func (p *CommentProxy) failCall(w http.ResponseWriter, op, repo string, err error) {
	switch {
	case errors.Is(err, errUnbound):
		p.fail(w, op, repo, http.StatusNotFound, "repository_not_bound", nil)
	case errors.Is(err, errGitHubNotFound):
		p.fail(w, op, repo, http.StatusNotFound, "not_found", nil)
	case errors.Is(err, errNotAPR):
		p.fail(w, op, repo, http.StatusUnprocessableEntity, "not_a_pull_request", nil)
	case errors.Is(err, errNotOwn):
		p.fail(w, op, repo, http.StatusForbidden, "not_the_reviewers_comment", nil)
	default:
		p.fail(w, op, repo, http.StatusBadGateway, "github_comment_failed", err)
	}
}

func (p *CommentProxy) fail(w http.ResponseWriter, op, repo string, status int, code string, err error) {
	if err != nil {
		p.logger().Warn("reviewer comment refused", "op", op, "repository", repo, "status", status, "err", err)
	}
	writeJSON(w, status, map[string]string{"factory_error": code})
}

func (p *CommentProxy) logger() *slog.Logger {
	if p.Logger != nil {
		return p.Logger
	}
	return slog.Default()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
