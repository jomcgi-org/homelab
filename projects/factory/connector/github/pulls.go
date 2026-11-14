package github

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// MaxListed is the page size of one listing; a full page means GitHub may
// hold more than was read.
const MaxListed = 100

// ListPullRequests lists the open PRs into base, oldest first, the first
// MaxListed.
func (c *Client) ListPullRequests(ctx context.Context, repo, base string) ([]PullRequest, error) {
	q := url.Values{
		"state": {"open"}, "base": {base}, "sort": {"created"}, "direction": {"asc"},
		"per_page": {strconv.Itoa(MaxListed)},
	}
	var pulls []restPull
	if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
		return nil, err
	}
	out := make([]PullRequest, 0, len(pulls))
	for _, p := range pulls {
		out = append(out, *p.pr())
	}
	return out, nil
}

// PullRequestFile is one file a PR changes. PreviousPath is set for a
// rename.
type PullRequestFile struct {
	Path         string `json:"filename"`
	PreviousPath string `json:"previous_filename"`
	Status       string `json:"status"` // added | removed | modified | renamed | copied | changed | unchanged
	Additions    int    `json:"additions"`
	Deletions    int    `json:"deletions"`
}

// MaxFilePages bounds a PR file listing: GitHub lists at most 3000 files.
const MaxFilePages = 30

// PullRequestFiles lists the files a PR changes, page by page, up to the
// 3000 GitHub lists.
func (c *Client) PullRequestFiles(ctx context.Context, repo string, number int) ([]PullRequestFile, error) {
	var all []PullRequestFile
	for page := 1; page <= MaxFilePages; page++ {
		var out []PullRequestFile
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		path := repoPath(repo) + "/pulls/" + strconv.Itoa(number) + "/files?" + q.Encode()
		if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
			return nil, err
		}
		all = append(all, out...)
		if len(out) < MaxListed {
			break
		}
	}
	return all, nil
}

// escapePath escapes each segment of a slash-separated path.
func escapePath(p string) string {
	segs := strings.Split(p, "/")
	for i, s := range segs {
		segs[i] = url.PathEscape(s)
	}
	return strings.Join(segs, "/")
}

// BranchHead is the commit a branch points at.
func (c *Client) BranchHead(ctx context.Context, repo, branch string) (string, error) {
	var out struct {
		Object struct {
			SHA string `json:"sha"`
		} `json:"object"`
	}
	if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/git/ref/heads/"+escapePath(branch), nil, &out); err != nil {
		return "", err
	}
	if out.Object.SHA == "" {
		return "", errors.New("github: branch ref without a commit")
	}
	return out.Object.SHA, nil
}

// ErrTooLarge is a file the contents API does not return inline (over 1 MB).
var ErrTooLarge = errors.New("github: file too large to read inline")

// FileContent reads a file at ref. A missing file, or a path that is a
// directory, is ErrNotFound.
func (c *Client) FileContent(ctx context.Context, repo, path, ref string) ([]byte, error) {
	var raw json.RawMessage // a directory answers with an array
	p := repoPath(repo) + "/contents/" + escapePath(path) + "?" + url.Values{"ref": {ref}}.Encode()
	if err := c.do(ctx, repo, http.MethodGet, p, nil, &raw); err != nil {
		return nil, err
	}
	if len(raw) == 0 || raw[0] != '{' {
		return nil, ErrNotFound
	}
	var out struct {
		Type     string `json:"type"`
		Encoding string `json:"encoding"`
		Content  string `json:"content"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	switch {
	case out.Type != "file":
		return nil, ErrNotFound
	case out.Encoding != "base64":
		return nil, ErrTooLarge
	}
	return base64.StdEncoding.DecodeString(strings.NewReplacer("\n", "", "\r", "").Replace(out.Content))
}

// PullRequestQueue is a PR's current head and merge queue entry.
type PullRequestQueue struct {
	NodeID  string
	State   string // open | closed | merged
	Draft   bool
	HeadSHA string
	BaseRef string
	// MergeQueueState is the entry's state, empty when the PR is not queued.
	MergeQueueState string
}

const pullRequestQueueQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id state isDraft headRefOid baseRefName
      mergeQueueEntry { state }
    }
  }
}`

// PullRequestQueue reads a PR's head, draft state and merge queue entry with
// one small GraphQL query.
func (c *Client) PullRequestQueue(ctx context.Context, repo string, number int) (*PullRequestQueue, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			PullRequest *struct {
				ID              string `json:"id"`
				State           string `json:"state"`
				IsDraft         bool   `json:"isDraft"`
				HeadRefOid      string `json:"headRefOid"`
				BaseRefName     string `json:"baseRefName"`
				MergeQueueEntry *struct {
					State string `json:"state"`
				} `json:"mergeQueueEntry"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, pullRequestQueueQuery, map[string]any{"owner": owner, "name": name, "number": number}, &out); err != nil {
		return nil, err
	}
	p := out.Repository.PullRequest
	if p == nil {
		return nil, ErrNotFound
	}
	q := &PullRequestQueue{
		NodeID: p.ID, State: strings.ToLower(p.State), Draft: p.IsDraft, HeadSHA: p.HeadRefOid, BaseRef: p.BaseRefName,
	}
	if p.MergeQueueEntry != nil {
		q.MergeQueueState = p.MergeQueueEntry.State
	}
	return q, nil
}

// MergeQueueEntry is one PR waiting in a merge queue. HeadCommit is the
// merge group commit GitHub builds for it, empty until it exists.
type MergeQueueEntry struct {
	State      string
	HeadCommit string
	Number     int
	PRHeadSHA  string
}

const mergeQueueQuery = `query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      entries(first: 100) {
        nodes {
          state
          headCommit { oid }
          pullRequest { number headRefOid }
        }
      }
    }
  }
}`

// MergeQueueEntries lists the first 100 entries of branch's merge queue; a
// branch without a queue has none.
func (c *Client) MergeQueueEntries(ctx context.Context, repo, branch string) ([]MergeQueueEntry, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			MergeQueue *struct {
				Entries struct {
					Nodes []struct {
						State       string `json:"state"`
						HeadCommit  *oid   `json:"headCommit"`
						PullRequest *struct {
							Number     int    `json:"number"`
							HeadRefOid string `json:"headRefOid"`
						} `json:"pullRequest"`
					} `json:"nodes"`
				} `json:"entries"`
			} `json:"mergeQueue"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, mergeQueueQuery, map[string]any{"owner": owner, "name": name, "branch": branch}, &out); err != nil {
		return nil, err
	}
	if out.Repository.MergeQueue == nil {
		return nil, nil
	}
	var entries []MergeQueueEntry
	for _, n := range out.Repository.MergeQueue.Entries.Nodes {
		if n.PullRequest == nil {
			continue
		}
		e := MergeQueueEntry{State: n.State, Number: n.PullRequest.Number, PRHeadSHA: n.PullRequest.HeadRefOid}
		if n.HeadCommit != nil {
			e.HeadCommit = n.HeadCommit.OID
		}
		entries = append(entries, e)
	}
	return entries, nil
}

const pullRequestHeadsQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(last: 100, itemTypes: [PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT]) {
        nodes {
          __typename
          ... on PullRequestCommit { commit { oid } }
          ... on HeadRefForcePushedEvent { beforeCommit { oid } afterCommit { oid } }
        }
      }
    }
  }
}`

// PullRequestHeads lists commits that may have been a PR's head, oldest
// first, from the last 100 commit and force-push events of its timeline: a
// pushed commit, and both sides of a force push.
func (c *Client) PullRequestHeads(ctx context.Context, repo string, number int) ([]string, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			PullRequest *struct {
				TimelineItems struct {
					Nodes []struct {
						Type         string `json:"__typename"`
						Commit       *oid   `json:"commit"`
						BeforeCommit *oid   `json:"beforeCommit"`
						AfterCommit  *oid   `json:"afterCommit"`
					} `json:"nodes"`
				} `json:"timelineItems"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, pullRequestHeadsQuery, map[string]any{"owner": owner, "name": name, "number": number}, &out); err != nil {
		return nil, err
	}
	p := out.Repository.PullRequest
	if p == nil {
		return nil, ErrNotFound
	}
	var heads []string
	for _, n := range p.TimelineItems.Nodes {
		for _, o := range []*oid{n.Commit, n.BeforeCommit, n.AfterCommit} {
			if o != nil && o.OID != "" {
				heads = append(heads, o.OID)
			}
		}
	}
	return heads, nil
}

// PullRequestReview is a submitted PR review, from REST.
type PullRequestReview struct {
	Author    string
	State     string // APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
	CommitSHA string
	Body      string
}

// PullRequestReviews lists a PR's first 100 reviews, oldest first.
func (c *Client) PullRequestReviews(ctx context.Context, repo string, number int) ([]PullRequestReview, error) {
	var out []struct {
		User     login  `json:"user"`
		State    string `json:"state"`
		CommitID string `json:"commit_id"`
		Body     string `json:"body"`
	}
	path := repoPath(repo) + "/pulls/" + strconv.Itoa(number) + "/reviews?per_page=" + strconv.Itoa(MaxListed)
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	reviews := make([]PullRequestReview, 0, len(out))
	for _, r := range out {
		reviews = append(reviews, PullRequestReview{Author: r.User.Login, State: r.State, CommitSHA: r.CommitID, Body: r.Body})
	}
	return reviews, nil
}

// Comment is a PR conversation comment.
type Comment struct {
	Author string
	Body   string
}

// PullRequestComments lists the first 100 conversation comments on a PR,
// oldest first.
func (c *Client) PullRequestComments(ctx context.Context, repo string, number int) ([]Comment, error) {
	var out []struct {
		User login  `json:"user"`
		Body string `json:"body"`
	}
	path := repoPath(repo) + "/issues/" + strconv.Itoa(number) + "/comments?per_page=" + strconv.Itoa(MaxListed)
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	comments := make([]Comment, 0, len(out))
	for _, cm := range out {
		comments = append(comments, Comment{Author: cm.User.Login, Body: cm.Body})
	}
	return comments, nil
}

// CommitComparison is what changed from one commit to another.
type CommitComparison struct {
	// Status is ahead when head descends from base, diverged after a rebase.
	Status string
	Files  []PullRequestFile
}

// CompareCommits lists the files that differ between two commits (the first
// 300 GitHub lists). A commit GitHub no longer has is ErrNotFound.
func (c *Client) CompareCommits(ctx context.Context, repo, base, head string) (*CommitComparison, error) {
	var out struct {
		Status string            `json:"status"`
		Files  []PullRequestFile `json:"files"`
	}
	path := repoPath(repo) + "/compare/" + url.PathEscape(base) + "..." + url.PathEscape(head) + "?per_page=100"
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	return &CommitComparison{Status: out.Status, Files: out.Files}, nil
}
