// invariant: FACT-13
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
	"slices"
	"strconv"
	"strings"
	"sync"
)

// MaxCommentBody is the largest comment body the proxy writes, in bytes.
// GitHub takes 65536 characters.
const MaxCommentBody = 65536

// JSON escaping turns one byte into at most six (\u0026).
const jsonExpansion = 6

// maxCommentRequest bounds a create or edit request: the largest body,
// escaped, plus the other fields.
const maxCommentRequest = jsonExpansion*MaxCommentBody + 4096

// maxCommentAnswer bounds an answer the proxy reads whole from GitHub: one
// comment or one pull request.
const maxCommentAnswer = 4 << 20

// FindPages bounds the search for the reviewer's comment: pages of 100,
// oldest first, so 10,000 comments.
const FindPages = 100

// maxFindPage bounds one listing page GitHub sends while the proxy searches
// it. The page is decoded one comment at a time and only the match is kept,
// so this bounds the read, not memory: 100 bodies of 65536 characters at
// six bytes each, plus GitHub's other fields.
const maxFindPage = 48 << 20

// CommentProxy writes the reviewer's PR conversation comments with the
// reviewer App's comment token, which GitHub requires to hold
// pull_requests write and which could therefore approve a PR. The token
// never leaves the broker. The proxy does three things with it: find the
// oldest comment Author wrote on a PR with a given marker, create one on a
// pull request, and edit one that Author wrote. Callers wrap it with TokenReview
// authorization.
type CommentProxy struct {
	// Serialize discovery and writes so overlapping reconciliations cannot create duplicate threads.
	inlineMu sync.Mutex
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
		p.find(w, r)
	case http.MethodPost:
		if r.URL.Query().Get("inline") == "1" {
			p.inline(w, r)
			return
		}
		p.create(w, r)
	case http.MethodPatch:
		p.update(w, r)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

// maxFindPrefix bounds the marker a search matches on.
const maxFindPrefix = 256

// find serves GET ?repository=owner/name&number=N&prefix=P: the oldest
// comment on the PR that Author wrote and whose body starts with P (the
// reviewer's comment marker), or null. It searches at most FindPages pages
// and stops at the first match. complete is false when it stopped at the
// bound with more comments left, so a missing comment is not proven absent.
func (p *CommentProxy) find(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	repo, prefix := q.Get("repository"), q.Get("prefix")
	number, err := strconv.Atoi(q.Get("number"))
	if err != nil || number <= 0 || prefix == "" || len(prefix) > maxFindPrefix {
		p.fail(w, "find", repo, http.StatusBadRequest, "bad_request", nil)
		return
	}
	for page := 1; page <= FindPages; page++ {
		v := url.Values{"per_page": {"100"}, "page": {strconv.Itoa(page)}}
		found, next, err := p.findOnPage(r.Context(), repo, "/issues/"+strconv.Itoa(number)+"/comments?"+v.Encode(), prefix)
		if err != nil {
			p.failCall(w, "find", repo, err)
			return
		}
		if found != nil {
			writeJSON(w, http.StatusOK, map[string]any{"comment": found.proxied(), "complete": true})
			return
		}
		if !next {
			writeJSON(w, http.StatusOK, map[string]any{"comment": nil, "complete": true})
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"comment": nil, "complete": false})
}

// findOnPage reads one listing page a comment at a time and keeps only the
// first Author wrote that starts with prefix. A page that does not end in
// its closing bracket is an error, so a truncated page never passes for a
// complete one.
func (p *CommentProxy) findOnPage(ctx context.Context, repo, path, prefix string) (found *ghComment, next bool, err error) {
	resp, err := p.send(ctx, repo, http.MethodGet, path, nil)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	if err := answerError(resp, http.MethodGet, path); err != nil {
		return nil, false, err
	}
	dec := json.NewDecoder(io.LimitReader(resp.Body, maxFindPage))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('[') {
		return nil, false, fmt.Errorf("GitHub comment page is not a list: %v", err)
	}
	for dec.More() {
		var c ghComment
		if err := dec.Decode(&c); err != nil {
			return nil, false, fmt.Errorf("decode GitHub comment page: %w", err)
		}
		if found == nil && strings.EqualFold(c.User.Login, p.Author) && strings.HasPrefix(c.Body, prefix) {
			found = &c
		}
	}
	if tok, err := dec.Token(); err != nil || tok != json.Delim(']') {
		return nil, false, fmt.Errorf("GitHub comment page is incomplete: %v", err)
	}
	return found, strings.Contains(resp.Header.Get("Link"), `rel="next"`), nil
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
	if err := p.call(ctx, in.Repository, http.MethodGet, "/pulls/"+strconv.Itoa(in.Number), nil, &pr); err != nil {
		if errors.Is(err, errGitHubNotFound) {
			err = errNotAPR
		}
		p.failCall(w, "create", in.Repository, err)
		return
	}
	var out ghComment
	if err := p.call(ctx, in.Repository, http.MethodPost, "/issues/"+strconv.Itoa(in.Number)+"/comments",
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
	if err := p.call(ctx, in.Repository, http.MethodGet, path, nil, &cur); err != nil {
		p.failCall(w, "update", in.Repository, err)
		return
	}
	if !strings.EqualFold(cur.User.Login, p.Author) || !strings.Contains(cur.HTMLURL, "/pull/") {
		p.failCall(w, "update", in.Repository, errNotOwn)
		return
	}
	var out ghComment
	if err := p.call(ctx, in.Repository, http.MethodPatch, path, map[string]string{"body": in.Body}, &out); err != nil {
		p.failCall(w, "update", in.Repository, err)
		return
	}
	writeJSON(w, http.StatusOK, out.proxied())
}

func validBody(b string) bool {
	return strings.TrimSpace(b) != "" && len(b) <= MaxCommentBody
}

func (p *CommentProxy) decode(w http.ResponseWriter, r *http.Request, op string, v any) bool {
	dec := json.NewDecoder(io.LimitReader(r.Body, maxCommentRequest))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		p.fail(w, op, "", http.StatusBadRequest, "bad_request", err)
		return false
	}
	return true
}

// send starts one REST request for a bound repository with the comment
// token.
func (p *CommentProxy) send(ctx context.Context, repo, method, path string, body any) (*http.Response, error) {
	tok, err := p.Minter.ConnectorToken(ctx, repo)
	if err != nil {
		return nil, err
	}
	var rd io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(data)
	}
	a := p.Minter.App
	endpoint := "/repos/" + repo + path
	if path == "/graphql" {
		endpoint = path
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(a.APIURL, "/")+endpoint, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+tok.Token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return a.client().Do(req)
}

// call sends one REST request and decodes its answer whole.
func (p *CommentProxy) call(ctx context.Context, repo, method, path string, body, out any) error {
	resp, err := p.send(ctx, repo, method, path, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if err := answerError(resp, method, path); err != nil {
		return err
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxCommentAnswer+1))
	if err != nil {
		return err
	}
	if len(data) > maxCommentAnswer {
		return fmt.Errorf("GitHub %s %s: answer over %d bytes", method, path, maxCommentAnswer)
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("decode GitHub answer: %w", err)
	}
	return nil
}

// answerError is nil for a 2xx answer.
func answerError(resp *http.Response, method, path string) error {
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return errGitHubNotFound
	case resp.StatusCode < 200 || resp.StatusCode > 299:
		var msg struct {
			Message string `json:"message"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&msg)
		return fmt.Errorf("GitHub %s %s: HTTP %d: %s", method, strings.SplitN(path, "?", 2)[0], resp.StatusCode, msg.Message)
	}
	return nil
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

type inlineRequest struct {
	Repository string
	Number     int
	Head       string
	Findings   []struct {
		Key, Path, Body string
		Start, End      int
	}
	Active []string
	Fixed  []string
}

const inlinePrefix = "<!-- factory-review:inline "

type inlineThread struct {
	ID         string
	IsResolved bool
	Comments   struct {
		Nodes []struct {
			URL            string
			Body           string
			Author         struct{ Login string }
			OriginalCommit struct{ Oid string }
		}
	}
}

func (p *CommentProxy) graphql(ctx context.Context, repo, query string, vars map[string]any, out any) error {
	var resp struct {
		Data   json.RawMessage
		Errors []json.RawMessage
	}
	if err := p.call(ctx, repo, http.MethodPost, "/graphql", map[string]any{"query": query, "variables": vars}, &resp); err != nil {
		return err
	}
	if len(resp.Errors) > 0 {
		return errors.New("GitHub review thread operation failed")
	}
	if out != nil {
		return json.Unmarshal(resp.Data, out)
	}
	return nil
}

func (p *CommentProxy) threads(ctx context.Context, in inlineRequest) (map[string]inlineThread, int, error) {
	owner, name, ok := strings.Cut(in.Repository, "/")
	if !ok {
		return nil, 0, errNotAPR
	}
	vars := map[string]any{"owner": owner, "repo": name, "number": in.Number}
	threads := map[string]inlineThread{}
	count := 0
	for page := 0; page < FindPages; page++ {
		var data struct {
			Repository struct {
				PullRequest struct {
					HeadRefOid    string
					ReviewThreads struct {
						Nodes    []inlineThread
						PageInfo struct {
							HasNextPage bool
							EndCursor   string
						}
					}
				}
			}
		}
		err := p.graphql(ctx, in.Repository, `query($owner:String!,$repo:String!,$number:Int!,$after:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){headRefOid reviewThreads(first:20,after:$after){nodes{id isResolved comments(first:1){nodes{url body author{login} originalCommit{oid}}}} pageInfo{hasNextPage endCursor}}}}}`, vars, &data)
		if err != nil {
			return nil, 0, err
		}
		pr := data.Repository.PullRequest
		if pr.HeadRefOid != in.Head {
			return nil, 0, errors.New("review head has changed")
		}
		for _, t := range pr.ReviewThreads.Nodes {
			if len(t.Comments.Nodes) == 0 {
				continue
			}
			root := t.Comments.Nodes[0]
			marker, _, _ := strings.Cut(root.Body, "\n")
			key, found := strings.CutPrefix(marker, inlinePrefix)
			if !found || !strings.HasSuffix(key, " -->") || !strings.EqualFold(root.Author.Login, p.Author) {
				continue
			}
			key = strings.TrimSuffix(key, " -->")
			if existing, ok := threads[key]; !ok || existing.IsResolved || !t.IsResolved {
				threads[key] = t
			}
			if root.OriginalCommit.Oid == in.Head {
				count++
			}
		}
		if !pr.ReviewThreads.PageInfo.HasNextPage {
			return threads, count, nil
		}
		vars["after"] = pr.ReviewThreads.PageInfo.EndCursor
	}
	return nil, 0, errors.New("incomplete review thread listing")
}

func (p *CommentProxy) inline(w http.ResponseWriter, r *http.Request) {
	var in inlineRequest
	if !p.decode(w, r, "inline", &in) {
		return
	}
	if in.Number <= 0 || in.Head == "" {
		p.fail(w, "inline", in.Repository, 400, "bad_request", nil)
		return
	}
	p.inlineMu.Lock()
	defer p.inlineMu.Unlock()
	ctx := r.Context()
	threads, count, err := p.threads(ctx, in)
	if err != nil {
		p.failCall(w, "inline", in.Repository, err)
		return
	}
	for key, t := range threads {
		if !slices.Contains(in.Fixed, key) || slices.Contains(in.Active, key) || t.IsResolved || t.Comments.Nodes[0].OriginalCommit.Oid == in.Head {
			continue
		}
		err = p.graphql(ctx, in.Repository, `mutation($id:ID!,$body:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}){comment{id}} resolveReviewThread(input:{threadId:$id}){thread{id}}}`, map[string]any{"id": t.ID, "body": "Cleared on " + in.Head + "."}, nil)
		if err != nil {
			p.failCall(w, "inline", in.Repository, err)
			return
		}
	}
	var comments []map[string]any
	for _, f := range in.Findings {
		if t, ok := threads[f.Key]; count >= 25 || ok && (!t.IsResolved || t.Comments.Nodes[0].OriginalCommit.Oid == in.Head) {
			continue
		}
		body := inlinePrefix + f.Key + " -->\n" + f.Body
		if len(f.Key) != 64 || strings.ContainsAny(f.Key, "\r\n") || !validBody(body) || f.Start < 1 || f.End < f.Start {
			p.fail(w, "inline", in.Repository, 400, "bad_finding", nil)
			return
		}
		c := map[string]any{"path": f.Path, "line": f.End, "side": "RIGHT", "body": body}
		if f.Start != f.End {
			c["start_line"] = f.Start
			c["start_side"] = "RIGHT"
		}
		comments = append(comments, c)
		count++
	}
	if len(comments) > 0 {
		var out struct{ ID int64 }
		err = p.call(ctx, in.Repository, http.MethodPost, fmt.Sprintf("/pulls/%d/reviews", in.Number), map[string]any{"commit_id": in.Head, "event": "COMMENT", "body": "Review findings on changed lines.", "comments": comments}, &out)
		if err == nil {
			threads, _, err = p.threads(ctx, in)
		}
		if err != nil {
			p.failCall(w, "inline", in.Repository, err)
			return
		}
	}
	links := map[string]int64{}
	for _, key := range in.Active {
		if t, ok := threads[key]; ok && !t.IsResolved {
			_, fragment, ok := strings.Cut(t.Comments.Nodes[0].URL, "#discussion_r")
			if ok {
				id, err := strconv.ParseInt(fragment, 10, 64)
				if err == nil {
					links[key] = id
				}
			}
		}
	}
	writeJSON(w, http.StatusOK, links)
}
