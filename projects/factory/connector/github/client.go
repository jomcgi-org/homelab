// Package github is the factory's GitHub client, shared by the connector and
// the reviewer: REST and GraphQL calls on one repository at a time, with
// installation tokens the broker mints for that repository. It finds, opens
// and updates PRs, reads reviews, review threads, check runs and statuses,
// writes check runs, enqueues PRs in the merge queue and observes their
// merge.
package github

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ErrNotFound is a 404, or a GraphQL NOT_FOUND error.
var ErrNotFound = errors.New("github: not found")

// ErrPRExists is GitHub's 422 for a second PR from the same head branch.
var ErrPRExists = errors.New("github: a pull request already exists for this branch")

// HTTPError carries any other non-2xx answer.
type HTTPError struct {
	Status int
	Body   string
}

func (e *HTTPError) Error() string { return fmt.Sprintf("github: HTTP %d: %s", e.Status, e.Body) }

// Tokens supplies installation tokens per repository.
type Tokens interface {
	Token(ctx context.Context, repository string) (string, error)
	Invalidate(repository string)
}

// Client calls the GitHub API.
type Client struct {
	// BaseURL is the REST root, https://api.github.com by default. GraphQL is
	// served at BaseURL + /graphql.
	BaseURL string
	HTTP    *http.Client
	Tokens  Tokens
	// ETags, when set, makes every REST GET conditional (etag.go).
	ETags *ETagCache
}

// PullRequest is the part of a PR the factory reads.
type PullRequest struct {
	Number  int
	NodeID  string
	URL     string
	State   string // open | closed
	Merged  bool
	Draft   bool
	HeadSHA string
	HeadRef string
	BaseRef string
	Title   string
	// Body, Author, Labels and UpdatedAt are filled from REST reads only.
	Body      string
	Author    string
	Labels    []string
	UpdatedAt time.Time
}

// NewPullRequest opens a PR.
type NewPullRequest struct {
	Title string
	Head  string // branch in the same repository
	Base  string
	Body  string
}

// PullRequestUpdate changes a PR; nil fields stay.
type PullRequestUpdate struct {
	Body  *string `json:"body,omitempty"`
	State *string `json:"state,omitempty"`
}

// Comparison is the base...head comparison of a branch.
type Comparison struct {
	AheadBy int
	// HeadSHA and HeadSubject describe the newest commit the comparison lists.
	HeadSHA     string
	HeadSubject string
}

// Review is a submitted PR review.
type Review struct {
	NodeID      string
	State       string // APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
	Author      string
	CommitSHA   string
	Body        string
	SubmittedAt time.Time
}

// ReviewThread is an inline review conversation.
type ReviewThread struct {
	NodeID   string
	Resolved bool
	Outdated bool
	Path     string
	Line     int
	Comments []ReviewComment
}

// ReviewComment is one comment in a review thread.
type ReviewComment struct {
	NodeID    string
	Author    string
	Body      string
	CommitSHA string
	URL       string
}

// PullRequestDetail is a PR's state, reviews and threads.
type PullRequestDetail struct {
	PullRequest
	MergedAt       time.Time
	MergeCommitSHA string
	// MergeQueueState is the merge queue entry's state, empty when the PR is
	// not in the queue.
	MergeQueueState string
	Reviews         []Review
	Threads         []ReviewThread
}

// CheckRun is a check run on a commit.
type CheckRun struct {
	ID         int64
	NodeID     string
	Name       string
	Status     string // queued | in_progress | completed
	Conclusion string // success | failure | neutral | cancelled | skipped | timed_out | action_required
	HeadSHA    string
	AppID      int64
	ExternalID string
	StartedAt  time.Time
	URL        string
	Title      string
	Summary    string
	Text       string
}

// Status is a commit status, the latest per context.
type Status struct {
	NodeID      string
	Context     string
	State       string // success | failure | error | pending
	Creator     string
	Description string
	URL         string
	SHA         string
}

func (c *Client) base() string {
	if c.BaseURL == "" {
		return "https://api.github.com"
	}
	return strings.TrimSuffix(c.BaseURL, "/")
}

// do sends one call with the repository's token. A 401 drops the cached
// token and tries once more with a fresh one.
func (c *Client) do(ctx context.Context, repo, method, path string, body, out any) error {
	return c.send(ctx, repo, method, path, body, out, true)
}

// doUncached is do without the ETag cache, for large reads of immutable
// resources that would only crowd it.
func (c *Client) doUncached(ctx context.Context, repo, method, path string, body, out any) error {
	return c.send(ctx, repo, method, path, body, out, false)
}

func (c *Client) send(ctx context.Context, repo, method, path string, body, out any, cache bool) error {
	for try := 0; ; try++ {
		err := c.once(ctx, repo, method, path, body, out, cache)
		var h *HTTPError
		if try == 0 && errors.As(err, &h) && h.Status == http.StatusUnauthorized {
			c.Tokens.Invalidate(repo)
			continue
		}
		return err
	}
}

func (c *Client) once(ctx context.Context, repo, method, path string, body, out any, cache bool) error {
	tok, err := c.Tokens.Token(ctx, repo)
	if err != nil {
		return err
	}
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base()+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	var cached []byte
	conditional := cache && method == http.MethodGet && c.ETags != nil
	if conditional {
		if etag, b, ok := c.ETags.get(path); ok {
			req.Header.Set("If-None-Match", etag)
			cached = b
		}
	}
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	status := resp.StatusCode
	if conditional {
		switch {
		case status == http.StatusNotModified && cached != nil:
			// Unchanged since the cached read; GitHub charges no rate limit.
			c.ETags.observe(true)
			data, status = cached, http.StatusOK
		case status == http.StatusOK:
			if cached != nil {
				c.ETags.observe(false)
			}
			if etag := resp.Header.Get("ETag"); etag != "" {
				c.ETags.put(path, etag, data)
			}
		}
	}
	switch {
	case status == http.StatusNotFound:
		return ErrNotFound
	case status == http.StatusUnprocessableEntity && bytes.Contains(data, []byte("already exists")):
		return ErrPRExists
	case status < 200 || status > 299:
		return &HTTPError{Status: status, Body: truncate(string(data), 300)}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

func repoPath(repo string) string {
	owner, name, _ := strings.Cut(repo, "/")
	return "/repos/" + url.PathEscape(owner) + "/" + url.PathEscape(name)
}

type restPull struct {
	Number    int        `json:"number"`
	NodeID    string     `json:"node_id"`
	HTMLURL   string     `json:"html_url"`
	State     string     `json:"state"`
	Draft     bool       `json:"draft"`
	Title     string     `json:"title"`
	Body      *string    `json:"body"`
	MergedAt  *string    `json:"merged_at"`
	UpdatedAt *time.Time `json:"updated_at"`
	Merged    bool       `json:"merged"`
	User      login      `json:"user"`
	Labels    []struct {
		Name string `json:"name"`
	} `json:"labels"`
	Head struct {
		Ref string `json:"ref"`
		SHA string `json:"sha"`
	} `json:"head"`
	Base struct {
		Ref string `json:"ref"`
	} `json:"base"`
}

func (p restPull) pr() *PullRequest {
	pr := &PullRequest{
		Number: p.Number, NodeID: p.NodeID, URL: p.HTMLURL, State: p.State, Draft: p.Draft, Title: p.Title,
		Merged: p.Merged || p.MergedAt != nil, HeadSHA: p.Head.SHA, HeadRef: p.Head.Ref, BaseRef: p.Base.Ref,
		Author: p.User.Login,
	}
	if p.Body != nil {
		pr.Body = *p.Body
	}
	if p.UpdatedAt != nil {
		pr.UpdatedAt = *p.UpdatedAt
	}
	for _, l := range p.Labels {
		pr.Labels = append(pr.Labels, l.Name)
	}
	return pr
}

// FindPullRequest returns the PR whose head is branch in repo, or nil. An
// open PR wins; otherwise the newest merged, then the newest closed one.
func (c *Client) FindPullRequest(ctx context.Context, repo, branch string) (*PullRequest, error) {
	owner, _, _ := strings.Cut(repo, "/")
	q := url.Values{"head": {owner + ":" + branch}, "state": {"all"}, "per_page": {"100"}}
	var pulls []restPull
	if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
		return nil, err
	}
	var best *PullRequest
	rank := func(p *PullRequest) int {
		switch {
		case p.State == "open":
			return 2
		case p.Merged:
			return 1
		}
		return 0
	}
	for _, rp := range pulls {
		p := rp.pr()
		if p.HeadRef != branch {
			continue
		}
		if best == nil || rank(p) > rank(best) || (rank(p) == rank(best) && p.Number > best.Number) {
			best = p
		}
	}
	return best, nil
}

// CreatePullRequest opens a PR. A PR that already exists for the head is
// ErrPRExists; the caller finds it instead.
func (c *Client) CreatePullRequest(ctx context.Context, repo string, n NewPullRequest) (*PullRequest, error) {
	var out restPull
	body := map[string]any{"title": n.Title, "head": n.Head, "base": n.Base, "body": n.Body, "maintainer_can_modify": false}
	if err := c.do(ctx, repo, http.MethodPost, repoPath(repo)+"/pulls", body, &out); err != nil {
		return nil, err
	}
	return out.pr(), nil
}

// UpdatePullRequest edits a PR's body or state.
func (c *Client) UpdatePullRequest(ctx context.Context, repo string, number int, u PullRequestUpdate) (*PullRequest, error) {
	var out restPull
	if err := c.do(ctx, repo, http.MethodPatch, repoPath(repo)+"/pulls/"+strconv.Itoa(number), u, &out); err != nil {
		return nil, err
	}
	return out.pr(), nil
}

// Compare reports how far head is ahead of base. A missing branch is
// ErrNotFound.
func (c *Client) Compare(ctx context.Context, repo, base, head string) (*Comparison, error) {
	var out struct {
		AheadBy int `json:"ahead_by"`
		Commits []struct {
			SHA    string `json:"sha"`
			Commit struct {
				Message string `json:"message"`
			} `json:"commit"`
		} `json:"commits"`
	}
	path := repoPath(repo) + "/compare/" + url.PathEscape(base) + "..." + url.PathEscape(head) + "?per_page=100"
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	cmp := &Comparison{AheadBy: out.AheadBy}
	if n := len(out.Commits); n > 0 {
		last := out.Commits[n-1]
		cmp.HeadSHA = last.SHA
		cmp.HeadSubject, _, _ = strings.Cut(last.Commit.Message, "\n")
	}
	return cmp, nil
}

// CheckRuns lists the latest check run per name on a commit.
func (c *Client) CheckRuns(ctx context.Context, repo, sha string) ([]CheckRun, error) {
	return c.ListCheckRuns(ctx, repo, sha, CheckRunFilter{})
}

// Statuses lists the latest commit status per context. It reads the list
// endpoint, newest first, and keeps each context's first entry: the combined
// /commits/{sha}/status endpoint omits each status's creator, so a check
// bound to a statusCreator could never match it. Statuses past the first
// 100 are not read.
func (c *Client) Statuses(ctx context.Context, repo, sha string) ([]Status, error) {
	var out []struct {
		NodeID      string `json:"node_id"`
		Context     string `json:"context"`
		State       string `json:"state"`
		Description string `json:"description"`
		TargetURL   string `json:"target_url"`
		Creator     struct {
			Login string `json:"login"`
		} `json:"creator"`
	}
	path := repoPath(repo) + "/commits/" + url.PathEscape(sha) + "/statuses?per_page=100"
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	sts := make([]Status, 0, len(out))
	seen := make(map[string]bool, len(out))
	for _, s := range out {
		if seen[s.Context] {
			continue // an older status for a context already listed
		}
		seen[s.Context] = true
		sts = append(sts, Status{
			NodeID: s.NodeID, Context: s.Context, State: s.State, Creator: s.Creator.Login,
			Description: s.Description, URL: s.TargetURL, SHA: sha,
		})
	}
	return sts, nil
}

type gqlError struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

func (c *Client) graphql(ctx context.Context, repo, query string, vars map[string]any, out any) error {
	var resp struct {
		Data   json.RawMessage `json:"data"`
		Errors []gqlError      `json:"errors"`
	}
	if err := c.do(ctx, repo, http.MethodPost, "/graphql", map[string]any{"query": query, "variables": vars}, &resp); err != nil {
		return err
	}
	if len(resp.Errors) > 0 {
		e := resp.Errors[0]
		if e.Type == "NOT_FOUND" {
			return fmt.Errorf("%w: %s", ErrNotFound, e.Message)
		}
		return fmt.Errorf("github: graphql %s: %s", e.Type, e.Message)
	}
	return json.Unmarshal(resp.Data, out)
}

const pullRequestQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number url state merged mergedAt isDraft title headRefOid headRefName baseRefName
      mergeCommit { oid }
      mergeQueueEntry { state }
      reviews(last: 100) {
        nodes { id state body submittedAt author { login } commit { oid } }
      }
      reviewThreads(last: 100) {
        nodes {
          id isResolved isOutdated path line
          comments(first: 50) { nodes { id body url author { login } commit { oid } } }
        }
      }
    }
  }
}`

type login struct {
	Login string `json:"login"`
}

type oid struct {
	OID string `json:"oid"`
}

// PullRequestDetail reads a PR's state, merge queue entry, reviews and
// review threads with GraphQL.
func (c *Client) PullRequestDetail(ctx context.Context, repo string, number int) (*PullRequestDetail, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			PullRequest *struct {
				ID              string     `json:"id"`
				Number          int        `json:"number"`
				URL             string     `json:"url"`
				State           string     `json:"state"`
				Merged          bool       `json:"merged"`
				MergedAt        *time.Time `json:"mergedAt"`
				IsDraft         bool       `json:"isDraft"`
				Title           string     `json:"title"`
				HeadRefOid      string     `json:"headRefOid"`
				HeadRefName     string     `json:"headRefName"`
				BaseRefName     string     `json:"baseRefName"`
				MergeCommit     *oid       `json:"mergeCommit"`
				MergeQueueEntry *struct {
					State string `json:"state"`
				} `json:"mergeQueueEntry"`
				Reviews struct {
					Nodes []struct {
						ID          string     `json:"id"`
						State       string     `json:"state"`
						Body        string     `json:"body"`
						SubmittedAt *time.Time `json:"submittedAt"`
						Author      *login     `json:"author"`
						Commit      *oid       `json:"commit"`
					} `json:"nodes"`
				} `json:"reviews"`
				ReviewThreads struct {
					Nodes []struct {
						ID         string `json:"id"`
						IsResolved bool   `json:"isResolved"`
						IsOutdated bool   `json:"isOutdated"`
						Path       string `json:"path"`
						Line       *int   `json:"line"`
						Comments   struct {
							Nodes []struct {
								ID     string `json:"id"`
								Body   string `json:"body"`
								URL    string `json:"url"`
								Author *login `json:"author"`
								Commit *oid   `json:"commit"`
							} `json:"nodes"`
						} `json:"comments"`
					} `json:"nodes"`
				} `json:"reviewThreads"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, pullRequestQuery, map[string]any{"owner": owner, "name": name, "number": number}, &out); err != nil {
		return nil, err
	}
	p := out.Repository.PullRequest
	if p == nil {
		return nil, ErrNotFound
	}
	d := &PullRequestDetail{PullRequest: PullRequest{
		Number: p.Number, NodeID: p.ID, URL: p.URL, State: strings.ToLower(p.State), Merged: p.Merged,
		Draft: p.IsDraft, Title: p.Title, HeadSHA: p.HeadRefOid, HeadRef: p.HeadRefName, BaseRef: p.BaseRefName,
	}}
	if d.State == "merged" {
		d.State, d.Merged = "closed", true
	}
	if p.MergedAt != nil {
		d.MergedAt = *p.MergedAt
	}
	if p.MergeCommit != nil {
		d.MergeCommitSHA = p.MergeCommit.OID
	}
	if p.MergeQueueEntry != nil {
		d.MergeQueueState = p.MergeQueueEntry.State
	}
	for _, r := range p.Reviews.Nodes {
		rv := Review{NodeID: r.ID, State: r.State, Body: r.Body}
		if r.Author != nil {
			rv.Author = r.Author.Login
		}
		if r.Commit != nil {
			rv.CommitSHA = r.Commit.OID
		}
		if r.SubmittedAt != nil {
			rv.SubmittedAt = *r.SubmittedAt
		}
		d.Reviews = append(d.Reviews, rv)
	}
	sort.SliceStable(d.Reviews, func(i, j int) bool { return d.Reviews[i].SubmittedAt.Before(d.Reviews[j].SubmittedAt) })
	for _, t := range p.ReviewThreads.Nodes {
		th := ReviewThread{NodeID: t.ID, Resolved: t.IsResolved, Outdated: t.IsOutdated, Path: t.Path}
		if t.Line != nil {
			th.Line = *t.Line
		}
		for _, cm := range t.Comments.Nodes {
			rc := ReviewComment{NodeID: cm.ID, Body: cm.Body, URL: cm.URL}
			if cm.Author != nil {
				rc.Author = cm.Author.Login
			}
			if cm.Commit != nil {
				rc.CommitSHA = cm.Commit.OID
			}
			th.Comments = append(th.Comments, rc)
		}
		d.Threads = append(d.Threads, th)
	}
	return d, nil
}

const enqueueMutation = `mutation($id: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) {
    mergeQueueEntry { state }
  }
}`

// EnqueuePullRequest adds a PR to the merge queue, only while its head is
// headSHA, and returns the entry's state.
func (c *Client) EnqueuePullRequest(ctx context.Context, repo, nodeID, headSHA string) (string, error) {
	var out struct {
		EnqueuePullRequest struct {
			MergeQueueEntry *struct {
				State string `json:"state"`
			} `json:"mergeQueueEntry"`
		} `json:"enqueuePullRequest"`
	}
	if err := c.graphql(ctx, repo, enqueueMutation, map[string]any{"id": nodeID, "head": headSHA}, &out); err != nil {
		return "", err
	}
	if e := out.EnqueuePullRequest.MergeQueueEntry; e != nil {
		return e.State, nil
	}
	return "", errors.New("github: enqueuePullRequest returned no merge queue entry")
}

const dequeueMutation = `mutation($id: ID!) {
  dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { state } }
}`

// DequeuePullRequest removes a PR from the merge queue.
func (c *Client) DequeuePullRequest(ctx context.Context, repo, nodeID string) error {
	var out json.RawMessage
	return c.graphql(ctx, repo, dequeueMutation, map[string]any{"id": nodeID}, &out)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
