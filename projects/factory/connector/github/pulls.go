package github

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// MaxListed is the page size of one listing; a full page means GitHub may
// hold more than was read.
const MaxListed = 100

// RefActivity is a repository activity, newest first. Actor is the GitHub
// identity that pushed, independent of the commit's git author or committer.
type RefActivity struct {
	ID    int64  `json:"id"`
	Ref   string `json:"ref"`
	Type  string `json:"activity_type"`
	Actor struct {
		Login string `json:"login"`
	} `json:"actor"`
	Before    string    `json:"before"`
	After     string    `json:"after"`
	Timestamp time.Time `json:"timestamp"`
}

// HeadRefChangedAt returns GitHub's time for the latest change to this head.
// Unlike git commit dates, repository activity records when the ref changed.
// Check the exact ref and SHA so a push racing the PR listing cannot date a
// different head. Older branch history is not needed for queue ordering.
func (c *Client) HeadRefChangedAt(ctx context.Context, repo, branch, head string) (time.Time, error) {
	ref := "refs/heads/" + branch
	q := url.Values{"ref": {ref}, "direction": {"desc"}, "per_page": {"1"}, "page": {"1"}}
	var events []RefActivity
	if err := c.doUncached(ctx, repo, http.MethodGet, repoPath(repo)+"/activity?"+q.Encode(), nil, &events); err != nil {
		return time.Time{}, err
	}
	if len(events) != 1 {
		return time.Time{}, errors.New("github: missing latest ref activity")
	}
	event := events[0]
	if event.ID <= 0 || event.Ref != ref || head == "" || event.After != head || event.Before == event.After || event.Timestamp.IsZero() {
		return time.Time{}, errors.New("github: latest ref activity does not date the expected head")
	}
	switch event.Type {
	case "push", "force_push", "branch_creation", "pr_merge", "merge_queue_merge":
		return event.Timestamp, nil
	default:
		return time.Time{}, errors.New("github: latest ref activity is not a head change")
	}
}

const pullRequestReadySinceQuery = `query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      createdAt headRefOid baseRefName isDraft state
      labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
      timelineItems(last: 100, itemTypes: [READY_FOR_REVIEW_EVENT, UNLABELED_EVENT, REOPENED_EVENT, BASE_REF_CHANGED_EVENT]) {
        pageInfo { hasPreviousPage }
        nodes {
          __typename
          ... on ReadyForReviewEvent { createdAt }
          ... on UnlabeledEvent { createdAt label { name } }
          ... on ReopenedEvent { createdAt }
          ... on BaseRefChangedEvent { createdAt }
        }
      }
    }
  }
}`

// PullRequestReadySince bounds a recovered queue age by the last transition
// into eligibility. It reads the base repository even for fork PRs. Missing or
// truncated history cannot justify backdating the caller's observed wait.
func (c *Client) PullRequestReadySince(ctx context.Context, repo string, pr PullRequest, skipLabels []string) (time.Time, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			PullRequest *struct {
				CreatedAt   time.Time `json:"createdAt"`
				HeadRefOid  string    `json:"headRefOid"`
				BaseRefName string    `json:"baseRefName"`
				IsDraft     bool      `json:"isDraft"`
				State       string    `json:"state"`
				Labels      struct {
					PageInfo struct {
						HasNextPage bool `json:"hasNextPage"`
					} `json:"pageInfo"`
					Nodes []struct {
						Name string `json:"name"`
					} `json:"nodes"`
				} `json:"labels"`
				TimelineItems struct {
					PageInfo struct {
						HasPreviousPage bool `json:"hasPreviousPage"`
					} `json:"pageInfo"`
					Nodes []struct {
						Type      string    `json:"__typename"`
						CreatedAt time.Time `json:"createdAt"`
						Label     *struct {
							Name string `json:"name"`
						} `json:"label"`
					} `json:"nodes"`
				} `json:"timelineItems"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	if err := c.graphql(ctx, repo, pullRequestReadySinceQuery, map[string]any{"owner": owner, "name": name, "number": pr.Number}, &out); err != nil {
		return time.Time{}, err
	}
	p := out.Repository.PullRequest
	if p == nil || p.CreatedAt.IsZero() || p.HeadRefOid != pr.HeadSHA || p.BaseRefName != pr.BaseRef || p.IsDraft || p.State != "OPEN" || p.Labels.Nodes == nil || p.Labels.PageInfo.HasNextPage || p.TimelineItems.Nodes == nil || p.TimelineItems.PageInfo.HasPreviousPage {
		return time.Time{}, errors.New("github: incomplete or changed PR eligibility")
	}
	skipped := func(label string) bool {
		for _, skip := range skipLabels {
			if strings.EqualFold(label, skip) {
				return true
			}
		}
		return false
	}
	for _, label := range p.Labels.Nodes {
		if skipped(label.Name) {
			return time.Time{}, errors.New("github: PR is skipped")
		}
	}
	since := p.CreatedAt
	for _, event := range p.TimelineItems.Nodes {
		if event.CreatedAt.IsZero() {
			return time.Time{}, errors.New("github: missing eligibility event time")
		}
		switch event.Type {
		case "ReadyForReviewEvent", "ReopenedEvent", "BaseRefChangedEvent":
		case "UnlabeledEvent":
			if event.Label == nil || event.Label.Name == "" {
				return time.Time{}, errors.New("github: missing removed label")
			}
			if !skipped(event.Label.Name) {
				continue
			}
		default:
			return time.Time{}, errors.New("github: unexpected eligibility event")
		}
		if event.CreatedAt.After(since) {
			since = event.CreatedAt
		}
	}
	return since, nil
}

// HeadRefActivity reads at most 100 events through the ref's latest creation.
// Missing creation (including a truncated or missing page) is no proof. Read
// all activity types so force pushes and delete/recreate cycles cannot hide.
func (c *Client) HeadRefActivity(ctx context.Context, repo, branch string) ([]RefActivity, error) {
	ref := "refs/heads/" + branch
	q := url.Values{"ref": {ref}, "direction": {"desc"}, "per_page": {strconv.Itoa(MaxListed)}}
	var events []RefActivity
	if err := c.doUncached(ctx, repo, http.MethodGet, repoPath(repo)+"/activity?"+q.Encode(), nil, &events); err != nil {
		return nil, err
	}
	if len(events) > MaxListed {
		return nil, ErrTooMany
	}
	seen := map[int64]bool{}
	for i, event := range events {
		if event.ID <= 0 || seen[event.ID] || event.Ref != ref || event.Type == "" {
			return nil, errors.New("github: incomplete ref activity")
		}
		seen[event.ID] = true
		if event.Type == "branch_creation" {
			return events[:i+1], nil
		}
	}
	return nil, errors.New("github: ref activity does not reach its latest creation within 100 events")
}

// PullRequestCommit carries the immutable git author, not the linked GitHub user.
type PullRequestCommit struct {
	SHA    string `json:"sha"`
	Commit struct {
		Author struct {
			Name  string `json:"name"`
			Email string `json:"email"`
		} `json:"author"`
		// Message is the commit message, which the commit's author wrote.
		Message string `json:"message"`
	} `json:"commit"`
}

// PullRequestCommits refuses GitHub's 250-commit cap rather than returning a
// partial author list. A disappearing page or missing head also fails closed.
func (c *Client) PullRequestCommits(ctx context.Context, repo string, number int, head string) ([]PullRequestCommit, error) {
	var commits []PullRequestCommit
	seen := map[string]bool{}
	for page := 1; page <= 3; page++ {
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		var out []PullRequestCommit
		if err := c.doUncached(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls/"+strconv.Itoa(number)+"/commits?"+q.Encode(), nil, &out); err != nil {
			return nil, err
		}
		if out == nil || len(out) > MaxListed {
			return nil, errors.New("github: incomplete PR commits")
		}
		for _, cm := range out {
			if cm.SHA == "" || seen[cm.SHA] || cm.Commit.Author.Email == "" {
				return nil, errors.New("github: incomplete PR commit author")
			}
			seen[cm.SHA] = true
		}
		commits = append(commits, out...)
		if len(commits) >= 250 {
			return nil, ErrTooMany
		}
		if len(out) < MaxListed {
			if len(commits) == 0 || commits[len(commits)-1].SHA != head {
				return nil, errors.New("github: PR commits do not end at the expected head")
			}
			return commits, nil
		}
	}
	return nil, ErrTooMany
}

// MaxPullPages bounds an open PR listing: 10,000 PRs.
const MaxPullPages = 100

// GetPullRequest reads the source identity and body as well as its state.
func (c *Client) GetPullRequest(ctx context.Context, repo string, number int) (*PullRequest, error) {
	var out restPull
	if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls/"+strconv.Itoa(number), nil, &out); err != nil {
		return nil, err
	}
	if out.Number != number || out.User.Login == "" || out.State == "" {
		return nil, errors.New("github: incomplete PR identity")
	}
	return out.pr(), nil
}

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
	// Patch is the file's diff text. GitHub leaves it out or cuts it for a
	// large or binary file, so its line counts can fall short of the totals.
	Patch string `json:"patch"`
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
	App  *struct {
		ID int64 `json:"id"`
	} `json:"performed_via_github_app"`
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
	// Queued says the PR's timeline has an AddedToMergeQueueEvent; QueuedBy
	// is the actor of the latest one.
	Queued   bool
	QueuedBy string
	// AutoMergeEnabledBy is the actor of the latest AutoMergeEnabledEvent,
	// kept after the PR merges; AutoMergeBy is who holds the request while
	// it is still pending.
	AutoMergeEnabledBy string
	AutoMergeBy        string
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
      queued: timelineItems(itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT], last: 1) {
        filteredCount nodes { ... on AddedToMergeQueueEvent { actor { login } } }
      }
      autoMerge: timelineItems(itemTypes: [AUTO_MERGE_ENABLED_EVENT], last: 1) {
        filteredCount nodes { ... on AutoMergeEnabledEvent { actor { login } } }
      }
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
				Queued    timelineActor `json:"queued"`
				AutoMerge timelineActor `json:"autoMerge"`
				Commits   struct {
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
		Queued: p.Queued.FilteredCount > 0, QueuedBy: p.Queued.last(), AutoMergeEnabledBy: p.AutoMerge.last(),
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

// timelineActor is a timeline connection filtered to one event type.
type timelineActor struct {
	FilteredCount int `json:"filteredCount"`
	Nodes         []struct {
		Actor *login `json:"actor"`
	} `json:"nodes"`
}

// last is the latest event's actor, or "" when there is none.
func (t timelineActor) last() string {
	if n := len(t.Nodes); n > 0 && t.Nodes[n-1].Actor != nil {
		return t.Nodes[n-1].Actor.Login
	}
	return ""
}

// Revert forms that git and GitHub write, as in
// projects/monolith/factory/orchestration/factory_reverts.py: a "This reverts
// commit <sha>" line with a full or abbreviated SHA, a "Reverts [owner/repo]#N"
// line, or a subject that starts with "Revert" and cites #N. A qualified
// reference counts only when it names the scanned repository.
var (
	revertsSHA = regexp.MustCompile(`This reverts commit ([0-9a-f]{7,40})`)
	revertsPR  = regexp.MustCompile(`Reverts ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#([0-9]+)`)
	prRef      = regexp.MustCompile(`([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#([0-9]+)`)
)

// RevertsMerge reports whether one commit message on repo's base branch
// reverts the merge commit sha or pull request number.
func RevertsMerge(message, repo, sha string, number int) bool {
	if sha != "" {
		for _, m := range revertsSHA.FindAllStringSubmatch(message, -1) {
			if strings.HasPrefix(sha, m[1]) {
				return true
			}
		}
	}
	if number <= 0 {
		return false
	}
	refs := revertsPR.FindAllStringSubmatch(message, -1)
	subject, _, _ := strings.Cut(message, "\n")
	if strings.HasPrefix(subject, "Revert") || strings.HasPrefix(subject, "revert(") {
		refs = append(refs, prRef.FindAllStringSubmatch(subject, -1)...)
	}
	for _, m := range refs {
		if m[1] != "" && !strings.EqualFold(m[1], repo) {
			continue
		}
		if n, err := strconv.Atoi(m[2]); err == nil && n == number {
			return true
		}
	}
	return false
}

// RevertedMerge returns the first base-branch commit in the interval that
// reverts the merge commit sha or pull request number.
func (c *Client) RevertedMerge(ctx context.Context, repo, branch, sha string, number int, since, until time.Time, before func(int) error) (*time.Time, error) {
	q := url.Values{"sha": {branch}, "since": {since.UTC().Format(time.RFC3339)}, "until": {until.UTC().Format(time.RFC3339)}, "per_page": {"100"}}
	var earliest *time.Time
	for page := 1; ; page++ {
		q.Set("page", strconv.Itoa(page))
		var commits []struct {
			Commit struct {
				Message   string
				Committer struct{ Date time.Time }
			}
		}
		if err := c.send(ctx, repo, http.MethodGet, repoPath(repo)+"/commits?"+q.Encode(), nil, &commits, false, func() error { return before(page) }); err != nil {
			return nil, err
		}
		for _, commit := range commits {
			at := commit.Commit.Committer.Date
			if at.Before(since) || at.After(until) {
				continue
			}
			if RevertsMerge(commit.Commit.Message, repo, sha, number) && (earliest == nil || at.Before(*earliest)) {
				earliest = &at
			}
		}
		if len(commits) < MaxListed {
			return earliest, nil
		}
	}
}
