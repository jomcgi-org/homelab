package github

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// MaxListed is the page size of one listing; a full page means GitHub may
// hold more than was read.
const MaxListed = 100

// MaxPullPages bounds an open PR listing: 10,000 PRs.
const MaxPullPages = 100

// PullRequestCreationBoundary includes all PR states; zero means an empty list.
func (c *Client) PullRequestCreationBoundary(ctx context.Context, repo string) (time.Time, error) {
	var pulls []restPull
	q := url.Values{"state": {"all"}, "sort": {"created"}, "direction": {"desc"}, "per_page": {"1"}}
	if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
		return time.Time{}, err
	}
	if pulls == nil || len(pulls) > 1 || (len(pulls) == 1 && pulls[0].CreatedAt.IsZero()) {
		return time.Time{}, errors.New("github: incomplete PR creation boundary")
	}
	if len(pulls) == 0 {
		return time.Time{}, nil
	}
	return pulls[0].CreatedAt, nil
}

// ListPullRequests lists every open PR into base, oldest first, page by
// page. Pages are sorted by creation, so a new PR or push changes one page
// and the others stay answered by 304. complete is false only past
// MaxPullPages full pages. A PR that closes between two page reads shifts
// the next one off both pages, so one listing that leaves a PR out does not
// prove it closed.
func (c *Client) ListPullRequests(ctx context.Context, repo, base string) (prs []PullRequest, complete bool, err error) {
	for page := 1; page <= MaxPullPages; page++ {
		q := url.Values{
			"state": {"open"}, "base": {base}, "sort": {"created"}, "direction": {"asc"},
			"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)},
		}
		var pulls []restPull
		if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
			return nil, false, err
		}
		for _, p := range pulls {
			prs = append(prs, *p.pr())
		}
		if len(pulls) < MaxListed {
			return prs, true, nil
		}
	}
	return prs, false, nil
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

// Tree is the files of a commit's tree.
type Tree struct {
	Files map[string]bool
	// Truncated is set when GitHub listed only part of the tree (over
	// 100,000 entries or 7 MB); a path missing from Files may then exist.
	Truncated bool
}

// Has reports whether path is a file in the tree, or may be one when the
// listing was truncated.
func (t *Tree) Has(path string) bool { return t.Truncated || t.Files[path] }

// Tree lists every file at ref in one read. The answer is large and the
// tree of a commit never changes, so it bypasses the ETag cache.
func (c *Client) Tree(ctx context.Context, repo, ref string) (*Tree, error) {
	var out struct {
		Tree []struct {
			Path string `json:"path"`
			Type string `json:"type"`
		} `json:"tree"`
		Truncated bool `json:"truncated"`
	}
	p := repoPath(repo) + "/git/trees/" + url.PathEscape(ref) + "?recursive=1"
	if err := c.doUncached(ctx, repo, http.MethodGet, p, nil, &out); err != nil {
		return nil, err
	}
	t := &Tree{Files: make(map[string]bool, len(out.Tree)), Truncated: out.Truncated}
	for _, e := range out.Tree {
		if e.Type == "blob" {
			t.Files[e.Path] = true
		}
	}
	return t, nil
}

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
	Labels  []string
	// MergeQueueState is the entry's state, empty when the PR is not queued.
	MergeQueueState string
}

const pullRequestQueueQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id state isDraft headRefOid baseRefName
      mergeQueueEntry { state }
      labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
    }
  }
}`

// PullRequestQueue reads a PR's head, state, labels and merge queue entry.
// It fails when the label connection is absent or truncated.
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
				Labels *struct {
					Nodes []struct {
						Name string `json:"name"`
					} `json:"nodes"`
					PageInfo *struct {
						HasNextPage bool `json:"hasNextPage"`
					} `json:"pageInfo"`
				} `json:"labels"`
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
	if p.Labels == nil || p.Labels.PageInfo == nil || p.Labels.PageInfo.HasNextPage {
		return nil, fmt.Errorf("github: PR %d labels are incomplete", number)
	}
	q := &PullRequestQueue{
		NodeID: p.ID, State: strings.ToLower(p.State), Draft: p.IsDraft, HeadSHA: p.HeadRefOid, BaseRef: p.BaseRefName,
	}
	if p.MergeQueueEntry != nil {
		q.MergeQueueState = p.MergeQueueEntry.State
	}
	for _, label := range p.Labels.Nodes {
		q.Labels = append(q.Labels, label.Name)
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

const pullRequestHeadsQuery = `query($owner: String!, $name: String!, $number: Int!, $appId: Int!, $check: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(last: 100, itemTypes: [PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT]) {
        pageInfo { hasPreviousPage }
        nodes {
          __typename
          ... on PullRequestCommit { commit { ...checked } }
          ... on HeadRefForcePushedEvent { beforeCommit { ...checked } afterCommit { ...checked } }
        }
      }
    }
  }
}

fragment checked on Commit {
  oid
  checkSuites(first: 5, filterBy: {appId: $appId}) {
    pageInfo { hasNextPage }
    nodes {
      app { databaseId }
      checkRuns(last: 5, filterBy: {checkName: $check, status: COMPLETED}) { pageInfo { hasPreviousPage } nodes { name conclusion } }
    }
  }
}`

// Head is a commit that may have been a PR's head. Checked is set when it
// carries a completed, uncancelled check run of the filter's name and App,
// or when it has more of the App's check suites, or completed runs of that
// name, than one read lists, so one may be left out; the caller reads such
// a head's check runs.
type Head struct {
	SHA     string
	Checked bool
}

// HeadHistory lists a PR's possible heads, oldest first. Truncated is set
// when its timeline holds older events than were read.
type HeadHistory struct {
	Heads     []Head
	Truncated bool
}

// PullRequestHeads lists commits that may have been a PR's head, oldest
// first, from the last 100 commit and force-push events of its timeline (a
// pushed commit, and both sides of a force push), each marked by whether it
// carries a completed check run matching f, newest runs first. One GraphQL
// read finds the checked heads, however many commits each push added.
func (c *Client) PullRequestHeads(ctx context.Context, repo string, number int, f CheckRunFilter) (*HeadHistory, error) {
	owner, name, _ := strings.Cut(repo, "/")
	type commit struct {
		OID         string `json:"oid"`
		CheckSuites struct {
			PageInfo struct {
				HasNextPage bool `json:"hasNextPage"`
			} `json:"pageInfo"`
			Nodes []struct {
				App *struct {
					DatabaseID int64 `json:"databaseId"`
				} `json:"app"`
				CheckRuns struct {
					PageInfo struct {
						HasPreviousPage bool `json:"hasPreviousPage"`
					} `json:"pageInfo"`
					Nodes []struct {
						Name       string `json:"name"`
						Conclusion string `json:"conclusion"`
					} `json:"nodes"`
				} `json:"checkRuns"`
			} `json:"nodes"`
		} `json:"checkSuites"`
	}
	var out struct {
		Repository struct {
			PullRequest *struct {
				TimelineItems struct {
					PageInfo struct {
						HasPreviousPage bool `json:"hasPreviousPage"`
					} `json:"pageInfo"`
					Nodes []struct {
						Type         string  `json:"__typename"`
						Commit       *commit `json:"commit"`
						BeforeCommit *commit `json:"beforeCommit"`
						AfterCommit  *commit `json:"afterCommit"`
					} `json:"nodes"`
				} `json:"timelineItems"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	vars := map[string]any{"owner": owner, "name": name, "number": number, "appId": f.AppID, "check": f.Name}
	if err := c.graphql(ctx, repo, pullRequestHeadsQuery, vars, &out); err != nil {
		return nil, err
	}
	p := out.Repository.PullRequest
	if p == nil {
		return nil, ErrNotFound
	}
	h := &HeadHistory{Truncated: p.TimelineItems.PageInfo.HasPreviousPage}
	for _, n := range p.TimelineItems.Nodes {
		for _, cm := range []*commit{n.Commit, n.BeforeCommit, n.AfterCommit} {
			if cm == nil || cm.OID == "" {
				continue
			}
			head := Head{SHA: cm.OID, Checked: cm.CheckSuites.PageInfo.HasNextPage}
			for _, s := range cm.CheckSuites.Nodes {
				if s.App == nil || s.App.DatabaseID != f.AppID {
					continue
				}
				if s.CheckRuns.PageInfo.HasPreviousPage {
					head.Checked = true
				}
				for _, r := range s.CheckRuns.Nodes {
					if r.Name == f.Name && r.Conclusion != "" && !strings.EqualFold(r.Conclusion, "CANCELLED") {
						head.Checked = true
					}
				}
			}
			h.Heads = append(h.Heads, head)
		}
	}
	return h, nil
}

// PullRequestReview is a submitted PR review, from REST.
type PullRequestReview struct {
	ID          int64
	NodeID      string
	Author      string
	State       string // APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
	CommitSHA   string
	Body        string
	SubmittedAt time.Time
}

// MaxReviewPages bounds a PR's review listing: 3000 reviews.
const MaxReviewPages = 30

// ErrTooMany is a listing longer than the client reads; the caller must not
// act on the part it has.
var ErrTooMany = errors.New("github: listing longer than the client reads")

// PullRequestReviews lists every review of a PR, oldest first, page by
// page. Past MaxReviewPages full pages it fails with ErrTooMany, since the
// newest reviews would be the ones left out.
func (c *Client) PullRequestReviews(ctx context.Context, repo string, number int) ([]PullRequestReview, error) {
	var reviews []PullRequestReview
	for page := 1; page <= MaxReviewPages; page++ {
		var out []struct {
			ID          int64     `json:"id"`
			NodeID      string    `json:"node_id"`
			User        login     `json:"user"`
			State       string    `json:"state"`
			CommitID    string    `json:"commit_id"`
			Body        string    `json:"body"`
			SubmittedAt time.Time `json:"submitted_at"`
		}
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		path := repoPath(repo) + "/pulls/" + strconv.Itoa(number) + "/reviews?" + q.Encode()
		if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
			return nil, err
		}
		if out == nil {
			return nil, errors.New("github: missing review listing")
		}
		for _, r := range out {
			reviews = append(reviews, PullRequestReview{ID: r.ID, NodeID: r.NodeID, Author: r.User.Login, State: r.State, CommitSHA: r.CommitID, Body: r.Body, SubmittedAt: r.SubmittedAt})
		}
		if len(out) < MaxListed {
			return reviews, nil
		}
	}
	return nil, fmt.Errorf("%w: more than %d reviews on PR %d", ErrTooMany, MaxReviewPages*MaxListed, number)
}

// ReviewAcknowledgement binds body and edit provenance; empty logins stay unknown.
type ReviewAcknowledgement struct {
	Author       string
	Editor       string
	LastEditedAt *time.Time
	Body         string
	State        string
	CommitSHA    string
}

const pullRequestReviewAcknowledgementQuery = `query($id: ID!) {
  node(id: $id) {
    __typename
    ... on PullRequestReview {
      id author { login } editor { login } lastEditedAt body state commit { oid }
      pullRequest { number repository { nameWithOwner } }
    }
  }
}`

// PullRequestReviewAcknowledgement binds one review to its node, PR and editor.
func (c *Client) PullRequestReviewAcknowledgement(ctx context.Context, repo string, number int, nodeID string) (*ReviewAcknowledgement, error) {
	if repo == "" || number <= 0 || nodeID == "" {
		return nil, errors.New("github: incomplete review acknowledgement context")
	}
	var out struct {
		Node *struct {
			Type         string          `json:"__typename"`
			ID           string          `json:"id"`
			Author       *login          `json:"author"`
			Editor       json.RawMessage `json:"editor"`
			LastEditedAt json.RawMessage `json:"lastEditedAt"`
			Body         *string         `json:"body"`
			State        string          `json:"state"`
			Commit       *oid            `json:"commit"`
			PullRequest  *struct {
				Number     int `json:"number"`
				Repository *struct {
					NameWithOwner string `json:"nameWithOwner"`
				} `json:"repository"`
			} `json:"pullRequest"`
		} `json:"node"`
	}
	if err := c.graphql(ctx, repo, pullRequestReviewAcknowledgementQuery, map[string]any{"id": nodeID}, &out); err != nil {
		return nil, err
	}
	n := out.Node
	if n == nil {
		return nil, ErrNotFound
	}
	if n.Type != "PullRequestReview" || n.ID != nodeID || n.PullRequest == nil || n.PullRequest.Number != number ||
		n.PullRequest.Repository == nil || !strings.EqualFold(n.PullRequest.Repository.NameWithOwner, repo) {
		return nil, errors.New("github: review acknowledgement context mismatch")
	}
	// Null is valid; omitted editor/time cannot prove the body was never edited.
	if len(n.Editor) == 0 || len(n.LastEditedAt) == 0 || n.Body == nil || n.State == "" {
		return nil, errors.New("github: incomplete review acknowledgement")
	}
	result := &ReviewAcknowledgement{Body: *n.Body, State: n.State}
	var editor *login
	if err := json.Unmarshal(n.Editor, &editor); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(n.LastEditedAt, &result.LastEditedAt); err != nil {
		return nil, err
	}
	if n.Author != nil {
		result.Author = n.Author.Login
	}
	if editor != nil {
		result.Editor = editor.Login
	}
	if n.Commit != nil {
		result.CommitSHA = n.Commit.OID
	}
	return result, nil
}

// LabelEvent records additions; IDs do not order them against reviews.
type LabelEvent struct {
	ID        int64
	Label     string
	CreatedAt time.Time
}

// MaxIssueEventPages bounds all issue events read, including non-label events.
const MaxIssueEventPages = 30

// PullRequestLabelEvents returns complete additions or an error, never partial history.
func (c *Client) PullRequestLabelEvents(ctx context.Context, repo string, number int) ([]LabelEvent, error) {
	var events []LabelEvent
	for page := 1; page <= MaxIssueEventPages; page++ {
		var out []struct {
			ID        int64     `json:"id"`
			Event     string    `json:"event"`
			CreatedAt time.Time `json:"created_at"`
			Label     struct {
				Name string `json:"name"`
			} `json:"label"`
		}
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		path := repoPath(repo) + "/issues/" + strconv.Itoa(number) + "/events?" + q.Encode()
		if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
			return nil, err
		}
		if out == nil {
			return nil, errors.New("github: missing issue event listing")
		}
		for _, event := range out {
			if event.Event == "" {
				return nil, errors.New("github: missing issue event type")
			}
			if event.Event != "labeled" {
				continue
			}
			if event.ID <= 0 || event.Label.Name == "" || event.CreatedAt.IsZero() {
				return nil, errors.New("github: incomplete label event")
			}
			events = append(events, LabelEvent{ID: event.ID, Label: event.Label.Name, CreatedAt: event.CreatedAt})
		}
		if len(out) < MaxListed {
			return events, nil
		}
	}
	return nil, fmt.Errorf("%w: PR %d reached the %d-event read limit", ErrTooMany, number, MaxIssueEventPages*MaxListed)
}

// Comment is a PR conversation comment.
type Comment struct {
	ID     int64
	Author string
	Body   string
}

type restComment struct {
	ID   int64  `json:"id"`
	User login  `json:"user"`
	Body string `json:"body"`
}

func (cm restComment) comment() Comment {
	return Comment{ID: cm.ID, Author: cm.User.Login, Body: cm.Body}
}

// PullRequestComments lists the first 100 conversation comments on a PR,
// oldest first.
func (c *Client) PullRequestComments(ctx context.Context, repo string, number int) ([]Comment, error) {
	var out []restComment
	path := repoPath(repo) + "/issues/" + strconv.Itoa(number) + "/comments?per_page=" + strconv.Itoa(MaxListed)
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	comments := make([]Comment, 0, len(out))
	for _, cm := range out {
		comments = append(comments, cm.comment())
	}
	return comments, nil
}

// MaxCommentPages bounds a PR's conversation comment listing: 1000 comments.
const MaxCommentPages = 10

// IssueComments lists a PR's conversation comments, oldest first, page by
// page. complete is false when MaxCommentPages full pages were read, so more
// may follow.
func (c *Client) IssueComments(ctx context.Context, repo string, number int) (comments []Comment, complete bool, err error) {
	for page := 1; page <= MaxCommentPages; page++ {
		var out []restComment
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		path := repoPath(repo) + "/issues/" + strconv.Itoa(number) + "/comments?" + q.Encode()
		if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
			return nil, false, err
		}
		for _, cm := range out {
			comments = append(comments, cm.comment())
		}
		if len(out) < MaxListed {
			return comments, true, nil
		}
	}
	return comments, false, nil
}

// CreateIssueComment posts a conversation comment on a PR or issue.
func (c *Client) CreateIssueComment(ctx context.Context, repo string, number int, body string) (*Comment, error) {
	var out restComment
	path := repoPath(repo) + "/issues/" + strconv.Itoa(number) + "/comments"
	if err := c.do(ctx, repo, http.MethodPost, path, map[string]string{"body": body}, &out); err != nil {
		return nil, err
	}
	cm := out.comment()
	return &cm, nil
}

// UpdateIssueComment replaces a conversation comment's body. A deleted
// comment is ErrNotFound.
func (c *Client) UpdateIssueComment(ctx context.Context, repo string, id int64, body string) (*Comment, error) {
	var out restComment
	path := repoPath(repo) + "/issues/comments/" + strconv.FormatInt(id, 10)
	if err := c.do(ctx, repo, http.MethodPatch, path, map[string]string{"body": body}, &out); err != nil {
		return nil, err
	}
	cm := out.comment()
	return &cm, nil
}

// MaxCompareFiles is how many files GitHub lists in a comparison; it lists
// no more on later pages.
const MaxCompareFiles = 300

// CommitComparison is what changed from one commit to another.
type CommitComparison struct {
	// Status is ahead when head descends from base, diverged after a rebase.
	Status string
	Files  []PullRequestFile
	// Truncated is set when GitHub listed MaxCompareFiles files, so more may
	// differ.
	Truncated bool
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
	return &CommitComparison{Status: out.Status, Files: out.Files, Truncated: len(out.Files) >= MaxCompareFiles}, nil
}

// CitedPullRequest is fixed-class evidence about a pull request another PR
// cites: states, flags, times, commit IDs and account logins. It never holds
// a title, body, branch name or other text an author chose.
type CitedPullRequest struct {
	Number         int
	State          string // OPEN | CLOSED | MERGED
	Merged         bool
	MergedAt       time.Time
	MergeCommitSHA string
	BaseRef        string
	HeadSHA        string
	Author         string
	MergedBy       string
	// Queued says the PR's timeline has an AddedToMergeQueueEvent.
	Queued bool
	// AutoMergeBy is who requested auto-merge, while the request is pending.
	AutoMergeBy string
	// HeadCommitAuthor is the GitHub account behind the head commit's
	// author, and HeadSignature its signature state (VALID, ...), empty
	// when the head commit is unsigned or not the PR's last commit.
	HeadCommitAuthor   string
	HeadSignature      string
	HeadSignedByGitHub bool
}

const citedPullRequestQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number state merged mergedAt baseRefName headRefOid
      mergeCommit { oid }
      author { login }
      mergedBy { login }
      autoMergeRequest { enabledBy { login } }
      timelineItems(itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT], first: 1) { filteredCount }
      commits(last: 1) {
        nodes { commit { oid author { user { login } } signature { state wasSignedByGitHub } } }
      }
    }
  }
}`

// CitedPullRequest reads a pull request another PR cites. A number that
// names an issue or nothing gives ErrNotFound; reading through
// pullRequest(number:) needs no issues permission.
func (c *Client) CitedPullRequest(ctx context.Context, repo string, number int) (*CitedPullRequest, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			Item *struct {
				Number           int        `json:"number"`
				State            string     `json:"state"`
				Merged           bool       `json:"merged"`
				MergedAt         *time.Time `json:"mergedAt"`
				BaseRefName      string     `json:"baseRefName"`
				HeadRefOid       string     `json:"headRefOid"`
				MergeCommit      *oid       `json:"mergeCommit"`
				Author           *login     `json:"author"`
				MergedBy         *login     `json:"mergedBy"`
				AutoMergeRequest *struct {
					EnabledBy *login `json:"enabledBy"`
				} `json:"autoMergeRequest"`
				TimelineItems struct {
					FilteredCount int `json:"filteredCount"`
				} `json:"timelineItems"`
				Commits struct {
					Nodes []struct {
						Commit struct {
							OID    string `json:"oid"`
							Author *struct {
								User *login `json:"user"`
							} `json:"author"`
							Signature *struct {
								State             string `json:"state"`
								WasSignedByGitHub bool   `json:"wasSignedByGitHub"`
							} `json:"signature"`
						} `json:"commit"`
					} `json:"nodes"`
				} `json:"commits"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, citedPullRequestQuery, map[string]any{"owner": owner, "name": name, "number": number}, &out); err != nil {
		return nil, err
	}
	p := out.Repository.Item
	if p == nil {
		return nil, ErrNotFound
	}
	cp := &CitedPullRequest{
		Number: p.Number, State: p.State, Merged: p.Merged, BaseRef: p.BaseRefName, HeadSHA: p.HeadRefOid,
		Queued: p.TimelineItems.FilteredCount > 0,
	}
	if p.MergedAt != nil {
		cp.MergedAt = *p.MergedAt
	}
	if p.MergeCommit != nil {
		cp.MergeCommitSHA = p.MergeCommit.OID
	}
	if p.Author != nil {
		cp.Author = p.Author.Login
	}
	if p.MergedBy != nil {
		cp.MergedBy = p.MergedBy.Login
	}
	if p.AutoMergeRequest != nil && p.AutoMergeRequest.EnabledBy != nil {
		cp.AutoMergeBy = p.AutoMergeRequest.EnabledBy.Login
	}
	if n := p.Commits.Nodes; len(n) == 1 && n[0].Commit.OID == p.HeadRefOid {
		hc := n[0].Commit
		if hc.Author != nil && hc.Author.User != nil {
			cp.HeadCommitAuthor = hc.Author.User.Login
		}
		if hc.Signature != nil {
			cp.HeadSignature, cp.HeadSignedByGitHub = hc.Signature.State, hc.Signature.WasSignedByGitHub
		}
	}
	return cp, nil
}
