package review

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	brokerconfig "github.com/jomcgi/homelab/projects/factory/broker/config"
	brokergithub "github.com/jomcgi/homelab/projects/factory/broker/github"
	brokermetrics "github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
)

const (
	testRepo  = "example-org/homelab"
	testAppID = 5758326
	ciCreator = "buildbuddy-io[bot]"
	// botLogin writes comments made with the comment token.
	botLogin = "jomcgi-factory-review[bot]"
)

type fakePR struct {
	Number   int
	Title    string
	Body     string
	Author   string
	Head     string
	HeadRepo string
	HeadRef  string
	Activity []github.RefActivity
	Commits  []github.PullRequestCommit
	Base     string
	Draft    bool
	Labels   []string
	Updated  time.Time
	Created  time.Time
	Files    []github.PullRequestFile
	// Heads is the timeline's head history, oldest first; the current head
	// is appended when it changes.
	Heads          []string
	Reviews        []github.PullRequestReview
	LabelEvents    []github.LabelEvent
	ReviewEditors  map[int64]string
	ReviewEditedAt map[int64]time.Time
	Comments       []github.Comment
	// TimelineTruncated reports older timeline events than GitHub lists.
	TimelineTruncated bool
	Closed            bool
	// ClosedAt is when a closed PR closed; zero serves the current time.
	ClosedAt time.Time
}

type fakeCheck struct {
	ID          int64
	Name        string
	SHA         string
	AppID       int64
	Status      string
	Conclusion  string
	ExternalID  string
	StartedAt   time.Time
	Title       string
	Summary     string
	Text        string
	Annotations []github.Annotation
}

// fakeGitHub serves the REST and GraphQL calls the reviewer makes for one
// repository. Every GET carries an ETag and answers a match with 304.
type fakeGitHub struct {
	t        *testing.T
	mu       sync.Mutex
	prs      map[int]*fakePR
	checks   []*fakeCheck
	statuses map[string][]map[string]any
	baseHead string
	// baseFiles are the base branch's files at baseHead.
	baseFiles map[string]string
	// compares override the compare answer for "base...head".
	compares map[string]map[string]any
	queue    []github.MergeQueueEntry
	queued   map[int]string // PR number -> merge queue state
	// queueHead overrides the head the GraphQL PR read reports.
	queueHead map[int]string
	// cited answers the cited-PR read by number with a PullRequest's
	// GraphQL fields, or FORBIDDEN for a nil entry; any other number is
	// NOT_FOUND, as for an issue.
	cited       map[int]map[string]any
	enqueued    []string
	calls       []string
	gql         []string // GraphQL calls by kind
	contentRefs []string // ref:path of each file read, tree:ref of each tree read
	notModified int
	// refuseAnnotations answers a check run write with annotations 422.
	refuseAnnotations bool
	// ignoreAppFilter lists every App's check runs, as if GitHub dropped the
	// app_id filter, so only the reviewer's own matching keeps them apart.
	ignoreAppFilter bool
	// failCheckSHA rejects check creation for one head, leaving group writes available.
	failCheckSHA string
	// failCommentPage rejects one page of the reviewer's comment lookup.
	failCommentPage int
	// afterQueueRead changes metadata after its GraphQL snapshot was returned.
	// It runs once under mu, before the caller's subsequent open-PR listing.
	afterQueueRead   func()
	afterReviewsRead func()
	// fail answers "METHOD /path" with a status code; failGQL fails a
	// GraphQL call by kind.
	fail          map[string]int
	failGQL       map[string]bool
	nextID        int64
	nextEventID   int64
	nextCommentID int64
	now           func() time.Time
	// closeAfterPage closes PR [1] once the open PR listing's page [0] is
	// served, as a PR that closes between two page reads would.
	closeAfterPage [2]int
}

func newFakeGitHub(t *testing.T) (*fakeGitHub, *httptest.Server) {
	f := &fakeGitHub{
		t: t, prs: map[int]*fakePR{}, statuses: map[string][]map[string]any{}, baseHead: "base-1",
		baseFiles: map[string]string{}, queued: map[int]string{}, queueHead: map[int]string{}, cited: map[int]map[string]any{},
		fail: map[string]int{}, failGQL: map[string]bool{},
		now: time.Now,
	}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, srv
}

func (f *fakeGitHub) withLock(change func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	change()
}

func (f *fakeGitHub) addPR(pr *fakePR) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if pr.Base == "" {
		pr.Base = "main"
	}
	if pr.Author == "" {
		pr.Author = "jomcgi"
	}
	if pr.Created.IsZero() {
		pr.Created = f.now().UTC().Truncate(time.Second)
	}
	if len(pr.Heads) == 0 {
		pr.Heads = []string{pr.Head}
	}
	for _, label := range pr.Labels {
		f.nextEventID++
		pr.LabelEvents = append(pr.LabelEvents, github.LabelEvent{ID: f.nextEventID, Label: label, CreatedAt: f.now().UTC().Truncate(time.Second).Add(-time.Minute)})
	}
	f.prs[pr.Number] = pr
}

func (f *fakeGitHub) push(number int, sha string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	pr := f.prs[number]
	pr.Head = sha
	pr.Heads = append(pr.Heads, sha)
}

func (f *fakeGitHub) review(number int, author, state, sha, body string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	pr := f.prs[number]
	id := int64(number*100000 + len(pr.Reviews) + 1)
	pr.Reviews = append(pr.Reviews, github.PullRequestReview{ID: id, NodeID: fmt.Sprintf("REVIEW_%d", id), Author: author, State: state, CommitSHA: sha, Body: body, SubmittedAt: f.now().UTC().Truncate(time.Second)})
}

func (f *fakeGitHub) label(number int, labels ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	pr := f.prs[number]
	for _, label := range labels {
		if !containsFold(pr.Labels, label) {
			f.nextEventID++
			pr.LabelEvents = append(pr.LabelEvents, github.LabelEvent{ID: f.nextEventID, Label: label, CreatedAt: f.now().UTC().Truncate(time.Second)})
		}
	}
	pr.Labels = labels
}

// checksOn lists copies of every review check on sha, oldest first.
func (f *fakeGitHub) checksOn(sha string) []fakeCheck {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []fakeCheck
	for _, c := range f.checks {
		if c.SHA == sha && c.Name == "factory review" && c.AppID == testAppID {
			out = append(out, *c)
		}
	}
	return out
}

// addCheck adds a completed check run from any App.
func (f *fakeGitHub) addCheck(sha, name string, appID int64, conclusion string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.nextID++
	f.checks = append(f.checks, &fakeCheck{
		ID: f.nextID, Name: name, SHA: sha, AppID: appID, Status: "completed", Conclusion: conclusion,
		Title: "t", Summary: "s",
	})
}

func (f *fakeGitHub) failWith(call string, status int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if status == 0 {
		delete(f.fail, call)
	} else {
		f.fail[call] = status
	}
}

func (f *fakeGitHub) failGraphQL(kind string, fail bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failGQL[kind] = fail
}

func (f *fakeGitHub) failCheckWrites(sha string, status int) {
	f.failWith("POST /repos/"+testRepo+"/check-runs", status)
	f.failWith(fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, f.check(sha).ID), status)
}

func (f *fakeGitHub) setBaseFile(path, content string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.baseFiles[path] = content
}

func (f *fakeGitHub) setStatus(sha, context, state, creator string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	// Newest first, as GitHub lists them.
	f.statuses[sha] = append([]map[string]any{{
		"node_id": fmt.Sprintf("S_%d", len(f.statuses[sha])), "context": context, "state": state,
		"creator": map[string]any{"login": creator},
	}}, f.statuses[sha]...)
}

// check returns a copy of the latest review check on sha, or nil.
func (f *fakeGitHub) check(sha string) *fakeCheck {
	f.mu.Lock()
	defer f.mu.Unlock()
	var latest *fakeCheck
	for _, c := range f.checks {
		if c.SHA == sha && c.Name == "factory review" && c.AppID == testAppID && (latest == nil || c.ID > latest.ID) {
			latest = c
		}
	}
	if latest == nil {
		return nil
	}
	c := *latest
	return &c
}

func (f *fakeGitHub) checkCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.checks)
}

func (f *fakeGitHub) enqueues() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.enqueued...)
}

func (f *fakeGitHub) graphqlCalls(kind string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, k := range f.gql {
		if k == kind {
			n++
		}
	}
	return n
}

func (f *fakeGitHub) countCalls(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

func (f *fakeGitHub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	if r.Method == http.MethodPost && r.URL.Path == "/app/installations/1/access_tokens" {
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"token": "ghs_comment", "expires_at": time.Now().Add(time.Hour),
			"permissions":  map[string]string{"issues": "write", "pull_requests": "write"},
			"repositories": []map[string]string{{"full_name": testRepo}},
		})
		return
	}
	prefix := "/repos/" + testRepo
	path := strings.TrimPrefix(r.URL.Path, prefix)
	q := r.URL.Query()
	// The check token reads comments but cannot write them; the comment
	// token reaches comments and verifies the PR exists before creating one.
	comments := strings.HasPrefix(path, "/issues/") && strings.Contains(path, "comments")
	pullNumber, pullNumberErr := strconv.Atoi(strings.TrimPrefix(path, "/pulls/"))
	switch auth := r.Header.Get("Authorization"); {
	case auth == "Bearer ghs_comment" && (comments || (r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && pullNumberErr == nil)):
	case auth == "Bearer ghs_reviewer" && comments && r.Method != http.MethodGet:
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"Resource not accessible by integration"}`))
		return
	case auth != "Bearer ghs_reviewer":
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	if code := f.fail[r.Method+" "+r.URL.Path]; code != 0 {
		w.WriteHeader(code)
		_, _ = w.Write([]byte(`{"message":"injected failure"}`))
		return
	}
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/graphql":
		f.graphql(w, r)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && pullNumberErr == nil:
		if pr := f.prs[pullNumber]; pr != nil {
			f.get(w, r, f.pullJSON(pr))
			return
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && path == "/activity":
		for _, pr := range f.prs {
			if "refs/heads/"+pr.HeadRef == q.Get("ref") {
				f.get(w, r, page(pr.Activity, q))
				return
			}
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && strings.HasSuffix(path, "/commits"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/pulls/"), "/commits"))
		if pr := f.prs[n]; pr != nil {
			f.get(w, r, page(pr.Commits, q))
			return
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && path == "/pulls" && q.Get("state") == "all" && q.Get("sort") == "updated":
		out := []map[string]any{}
		for _, pr := range f.sortedPRs() {
			out = append(out, f.pullJSON(pr))
		}
		sort.SliceStable(out, func(i, j int) bool {
			return out[i]["updated_at"].(time.Time).After(out[j]["updated_at"].(time.Time))
		})
		f.get(w, r, page(out, q))
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/commits/") && strings.HasSuffix(path, "/pulls"):
		sha := strings.TrimSuffix(strings.TrimPrefix(path, "/commits/"), "/pulls")
		out := []map[string]any{}
		for _, pr := range f.sortedPRs() {
			if !pr.Closed && slices.Contains(pr.Heads, sha) {
				out = append(out, f.pullJSON(pr))
			}
		}
		f.get(w, r, page(out, q))
	case r.Method == http.MethodGet && path == "/pulls":
		if q.Get("state") == "all" {
			latest := time.Time{}
			for _, pr := range f.prs {
				if pr.Created.After(latest) {
					latest = pr.Created
				}
			}
			out := []map[string]any{}
			if !latest.IsZero() {
				out = append(out, map[string]any{"created_at": latest})
			}
			f.get(w, r, out)
			return
		}
		out := []map[string]any{}
		for _, pr := range f.sortedPRs() {
			if q.Get("state") != "open" || pr.Closed || pr.Base != q.Get("base") {
				continue
			}
			labels := []map[string]string{}
			for _, l := range pr.Labels {
				labels = append(labels, map[string]string{"name": l})
			}
			headRef := pr.HeadRef
			if headRef == "" {
				headRef = fmt.Sprintf("branch-%d", pr.Number)
			}
			out = append(out, map[string]any{
				"number": pr.Number, "node_id": fmt.Sprintf("PR_%d", pr.Number), "state": "open", "draft": pr.Draft,
				"title": pr.Title, "body": pr.Body, "user": map[string]string{"login": pr.Author}, "labels": labels,
				"html_url":   fmt.Sprintf("https://github.com/%s/pull/%d", testRepo, pr.Number),
				"updated_at": pr.Updated.UTC().Format(time.RFC3339),
				"created_at": pr.Created.UTC().Format(time.RFC3339),
				"head":       map[string]any{"ref": headRef, "sha": pr.Head, "repo": map[string]string{"full_name": pr.HeadRepo}},
				"base":       map[string]string{"ref": pr.Base},
			})
		}
		served := page(out, q)
		if c := f.closeAfterPage; c[1] != 0 && first(q["page"]) == strconv.Itoa(c[0]) {
			f.prs[c[1]].Closed, f.closeAfterPage = true, [2]int{}
		}
		f.get(w, r, served)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/commits/") && strings.HasSuffix(path, "/check-runs"):
		sha := strings.TrimSuffix(strings.TrimPrefix(path, "/commits/"), "/check-runs")
		latest := map[string]*fakeCheck{}
		for _, c := range f.checks {
			if c.SHA != sha || (q.Get("check_name") != "" && c.Name != q.Get("check_name")) ||
				(!f.ignoreAppFilter && q.Get("app_id") != "" && strconv.FormatInt(c.AppID, 10) != q.Get("app_id")) {
				continue
			}
			key := fmt.Sprintf("%s/%d", c.Name, c.AppID)
			if l := latest[key]; l == nil || c.ID > l.ID {
				latest[key] = c
			}
		}
		var ids []*fakeCheck
		for _, c := range latest {
			ids = append(ids, c)
		}
		sort.Slice(ids, func(i, j int) bool { return ids[i].ID < ids[j].ID })
		runs := []map[string]any{}
		for _, c := range ids {
			runs = append(runs, checkJSON(c))
		}
		f.get(w, r, map[string]any{"total_count": len(runs), "check_runs": runs})
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/commits/") && strings.HasSuffix(path, "/statuses"):
		sha := strings.TrimSuffix(strings.TrimPrefix(path, "/commits/"), "/statuses")
		out := f.statuses[sha]
		if out == nil {
			out = []map[string]any{}
		}
		f.get(w, r, out)
	case r.Method == http.MethodPost && path == "/check-runs":
		var body checkWrite
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.HeadSHA == f.failCheckSHA && f.failCheckSHA != "" {
			http.Error(w, "check creation failed", http.StatusBadGateway)
			return
		}
		f.nextID++
		c := &fakeCheck{ID: f.nextID, AppID: testAppID}
		if !f.apply(w, c, body) {
			f.nextID--
			return
		}
		f.checks = append(f.checks, c)
		_ = json.NewEncoder(w).Encode(checkJSON(c))
	case r.Method == http.MethodPatch && strings.HasPrefix(path, "/check-runs/"):
		id, _ := strconv.ParseInt(strings.TrimPrefix(path, "/check-runs/"), 10, 64)
		var body checkWrite
		_ = json.NewDecoder(r.Body).Decode(&body)
		for _, c := range f.checks {
			if c.ID == id {
				next := *c
				if f.apply(w, &next, body) {
					*c = next
					_ = json.NewEncoder(w).Encode(checkJSON(c))
				}
				return
			}
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && strings.HasSuffix(path, "/files"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/pulls/"), "/files"))
		pr := f.prs[n]
		page, _ := strconv.Atoi(q.Get("page"))
		per, _ := strconv.Atoi(q.Get("per_page"))
		out := []github.PullRequestFile{}
		if pr != nil && page >= 1 {
			lo := min((page-1)*per, len(pr.Files))
			out = pr.Files[lo:min(lo+per, len(pr.Files))]
		}
		f.get(w, r, out)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && strings.HasSuffix(path, "/reviews"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/pulls/"), "/reviews"))
		out := []map[string]any{}
		for _, rv := range f.prs[n].Reviews {
			out = append(out, map[string]any{"id": rv.ID, "node_id": rv.NodeID, "user": map[string]string{"login": rv.Author}, "state": rv.State, "commit_id": rv.CommitSHA, "body": rv.Body, "submitted_at": rv.SubmittedAt})
		}
		f.get(w, r, page(out, q))
		if f.afterReviewsRead != nil {
			hook := f.afterReviewsRead
			f.afterReviewsRead = nil
			hook()
		}
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/issues/") && strings.HasSuffix(path, "/events"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/events"))
		out := []map[string]any{}
		for _, event := range f.prs[n].LabelEvents {
			out = append(out, map[string]any{"id": event.ID, "event": "labeled", "label": map[string]string{"name": event.Label}, "created_at": event.CreatedAt})
		}
		f.get(w, r, page(out, q))
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/issues/comments/"):
		id, _ := strconv.ParseInt(strings.TrimPrefix(path, "/issues/comments/"), 10, 64)
		for _, pr := range f.prs {
			for _, c := range pr.Comments {
				if c.ID == id {
					f.get(w, r, map[string]any{
						"id": c.ID, "user": map[string]string{"login": c.Author}, "body": c.Body,
						"html_url": fmt.Sprintf("https://github.com/%s/pull/%d#issuecomment-%d", testRepo, pr.Number, c.ID),
					})
					return
				}
			}
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/") && strings.HasSuffix(path, "/comments"):
		f.get(w, r, []any{})
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/issues/") && strings.HasSuffix(path, "/comments"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		p, _ := strconv.Atoi(q.Get("page"))
		if p > 0 && p == f.failCommentPage {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		out := []map[string]any{}
		for _, c := range f.prs[n].Comments {
			out = append(out, map[string]any{"id": c.ID, "user": map[string]string{"login": c.Author}, "body": c.Body})
		}
		per, _ := strconv.Atoi(q.Get("per_page"))
		if max(p, 1)*per < len(out) {
			w.Header().Set("Link", fmt.Sprintf(`<%s?page=%d&per_page=%d>; rel="next"`, r.URL.Path, max(p, 1)+1, per))
		}
		f.get(w, r, page(out, q))
	case r.Method == http.MethodPost && strings.HasPrefix(path, "/issues/") && strings.HasSuffix(path, "/comments"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		var body struct {
			Body string `json:"body"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.nextCommentID++
		c := github.Comment{ID: 1000 + f.nextCommentID, Author: botLogin, Body: body.Body}
		f.prs[n].Comments = append(f.prs[n].Comments, c)
		_ = json.NewEncoder(w).Encode(map[string]any{"id": c.ID, "user": map[string]string{"login": c.Author}, "body": c.Body})
	case r.Method == http.MethodPatch && strings.HasPrefix(path, "/issues/comments/"):
		id, _ := strconv.ParseInt(strings.TrimPrefix(path, "/issues/comments/"), 10, 64)
		var body struct {
			Body string `json:"body"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		for _, pr := range f.prs {
			for i := range pr.Comments {
				if c := &pr.Comments[i]; c.ID == id {
					c.Body = body.Body
					_ = json.NewEncoder(w).Encode(map[string]any{"id": c.ID, "user": map[string]string{"login": c.Author}, "body": c.Body})
					return
				}
			}
		}
		http.NotFound(w, r)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/compare/"):
		base, head, _ := strings.Cut(strings.TrimPrefix(path, "/compare/"), "...")
		if c, ok := f.compares[base+"..."+head]; ok {
			f.get(w, r, c)
			return
		}
		var pr *fakePR
		for _, p := range f.prs {
			if p.Head == head {
				pr = p
			}
		}
		if pr == nil || !slices.Contains(pr.Heads, base) {
			http.NotFound(w, r)
			return
		}
		f.get(w, r, map[string]any{"status": "ahead", "files": []github.PullRequestFile{{Path: "interdiff-" + head[:4] + ".go", Status: "modified", Additions: 1}}})
	case r.Method == http.MethodGet && path == "/git/ref/heads/main":
		f.get(w, r, map[string]any{"ref": "refs/heads/main", "object": map[string]string{"sha": f.baseHead, "type": "commit"}})
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/git/trees/"):
		ref := strings.TrimPrefix(path, "/git/trees/")
		f.contentRefs = append(f.contentRefs, "tree:"+ref)
		if ref != f.baseHead || q.Get("recursive") != "1" {
			http.NotFound(w, r)
			return
		}
		var entries []map[string]string
		dirs := map[string]bool{}
		for p := range f.baseFiles {
			entries = append(entries, map[string]string{"path": p, "type": "blob"})
			for d := p; strings.Contains(d, "/"); {
				d = d[:strings.LastIndex(d, "/")]
				dirs[d] = true
			}
		}
		for d := range dirs {
			entries = append(entries, map[string]string{"path": d, "type": "tree"})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"sha": ref, "tree": entries, "truncated": false})
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/contents/"):
		p := strings.TrimPrefix(path, "/contents/")
		f.contentRefs = append(f.contentRefs, q.Get("ref")+":"+p)
		content, ok := f.baseFiles[p]
		if !ok || q.Get("ref") != f.baseHead {
			http.NotFound(w, r)
			return
		}
		f.get(w, r, map[string]any{"type": "file", "encoding": "base64", "content": base64.StdEncoding.EncodeToString([]byte(content))})
	default:
		f.t.Errorf("unexpected GitHub call %s %s", r.Method, r.URL)
		http.NotFound(w, r)
	}
}

type checkWrite struct {
	Name        string                 `json:"name"`
	HeadSHA     string                 `json:"head_sha"`
	ExternalID  string                 `json:"external_id"`
	Status      string                 `json:"status"`
	Conclusion  string                 `json:"conclusion"`
	StartedAt   *time.Time             `json:"started_at"`
	CompletedAt *time.Time             `json:"completed_at"`
	Output      *github.CheckRunOutput `json:"output"`
}

func (f *fakeGitHub) apply(w http.ResponseWriter, c *fakeCheck, b checkWrite) bool {
	if b.Output != nil && len(b.Output.Annotations) > 0 {
		if f.refuseAnnotations {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"message":"Invalid annotation path"}`))
			return false
		}
		if len(b.Output.Annotations) > github.MaxAnnotations {
			f.t.Errorf("%d annotations in one write", len(b.Output.Annotations))
		}
	}
	if b.Name != "" {
		c.Name = b.Name
	}
	if b.HeadSHA != "" {
		c.SHA = b.HeadSHA
	}
	if b.ExternalID != "" {
		c.ExternalID = b.ExternalID
	}
	if b.StartedAt != nil {
		c.StartedAt = *b.StartedAt
	}
	if b.Status != "" {
		c.Status = b.Status
	}
	if b.Conclusion != "" {
		c.Conclusion, c.Status = b.Conclusion, "completed"
	}
	if b.Output != nil {
		if b.Output.Title == "" || b.Output.Summary == "" {
			f.t.Errorf("output without title or summary: %+v", b.Output)
		}
		c.Title, c.Summary, c.Text = b.Output.Title, b.Output.Summary, b.Output.Text
		c.Annotations = append(c.Annotations, b.Output.Annotations...)
	}
	return true
}

func checkJSON(c *fakeCheck) map[string]any {
	m := map[string]any{
		"id": c.ID, "node_id": fmt.Sprintf("CR_%d", c.ID), "name": c.Name, "head_sha": c.SHA, "status": c.Status,
		"conclusion": nil, "external_id": c.ExternalID, "app": map[string]any{"id": c.AppID},
		"html_url": fmt.Sprintf("https://github.com/%s/runs/%d", testRepo, c.ID),
		"output":   map[string]any{"title": c.Title, "summary": c.Summary, "text": c.Text},
	}
	if c.Conclusion != "" {
		m["conclusion"] = c.Conclusion
	}
	if !c.StartedAt.IsZero() {
		m["started_at"] = c.StartedAt.UTC().Format(time.RFC3339Nano)
	}
	return m
}

// pullJSON is a PR as an all-state or commit listing shows it. A PR is
// updated when it opens, changes or closes.
func (f *fakeGitHub) pullJSON(pr *fakePR) map[string]any {
	updated := pr.Created
	if pr.Updated.After(updated) {
		updated = pr.Updated
	}
	state, closed := "open", any(nil)
	if pr.Closed {
		at := pr.ClosedAt
		if at.IsZero() {
			at = f.now().UTC()
		}
		if at.After(updated) {
			updated = at
		}
		state, closed = "closed", at
	}
	return map[string]any{
		"number": pr.Number, "state": state, "draft": pr.Draft, "created_at": pr.Created, "updated_at": updated, "closed_at": closed,
		"head": map[string]any{"sha": pr.Head}, "base": map[string]string{"ref": pr.Base},
	}
}

func (f *fakeGitHub) sortedPRs() []*fakePR {
	var out []*fakePR
	for _, pr := range f.prs {
		out = append(out, pr)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Number < out[j].Number })
	return out
}

// page cuts a listing to the page and per_page a request names.
func page[T any](all []T, q map[string][]string) []T {
	n, _ := strconv.Atoi(first(q["page"]))
	per, _ := strconv.Atoi(first(q["per_page"]))
	if n < 1 || per < 1 {
		return all
	}
	lo := min((n-1)*per, len(all))
	return all[lo:min(lo+per, len(all))]
}

func first(v []string) string {
	if len(v) == 0 {
		return ""
	}
	return v[0]
}

// get answers a GET with an ETag over the body.
func (f *fakeGitHub) get(w http.ResponseWriter, r *http.Request, v any) {
	b, _ := json.Marshal(v)
	sum := sha256.Sum256(b)
	etag := `W/"` + hex.EncodeToString(sum[:8]) + `"`
	if r.Header.Get("If-None-Match") == etag {
		f.notModified++
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("ETag", etag)
	_, _ = w.Write(b)
}

func (f *fakeGitHub) graphql(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Query     string         `json:"query"`
		Variables map[string]any `json:"variables"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	reply := func(data any) { _ = json.NewEncoder(w).Encode(map[string]any{"data": data}) }
	number := 0
	if n, ok := req.Variables["number"].(float64); ok {
		number = int(n)
	}
	kind := ""
	defer func() { f.gql = append(f.gql, kind) }()
	for k, fail := range f.failGQL {
		if fail && strings.Contains(req.Query, map[string]string{
			"cited": "ADDED_TO_MERGE_QUEUE_EVENT", "enqueue": "enqueuePullRequest", "heads": "timelineItems", "pull_request": "mergeQueueEntry {", "merge_queue": "mergeQueue(branch", "review_acknowledgement": "... on PullRequestReview",
		}[k]) {
			kind = k
			_ = json.NewEncoder(w).Encode(map[string]any{"errors": []map[string]string{{"type": "FORBIDDEN", "message": "injected failure"}}})
			return
		}
	}
	switch {
	case strings.Contains(req.Query, "ADDED_TO_MERGE_QUEUE_EVENT"):
		kind = "cited"
		item, ok := f.cited[number]
		if ok && item == nil {
			_ = json.NewEncoder(w).Encode(map[string]any{"errors": []map[string]string{{"type": "FORBIDDEN", "message": "injected failure"}}})
			return
		}
		if !ok {
			_ = json.NewEncoder(w).Encode(map[string]any{
				"data":   map[string]any{"repository": map[string]any{"pullRequest": nil}},
				"errors": []map[string]string{{"type": "NOT_FOUND", "message": "no such pull request"}},
			})
			return
		}
		reply(map[string]any{"repository": map[string]any{"pullRequest": item}})
	case strings.Contains(req.Query, "... on PullRequestReview"):
		kind = "review_acknowledgement"
		id, _ := req.Variables["id"].(string)
		for number, pr := range f.prs {
			for _, rv := range pr.Reviews {
				if rv.NodeID != id {
					continue
				}
				var editor, editedAt any
				if login := pr.ReviewEditors[rv.ID]; login != "" {
					editor = map[string]string{"login": login}
				}
				if tm, ok := pr.ReviewEditedAt[rv.ID]; ok {
					editedAt = tm
				}
				reply(map[string]any{"node": map[string]any{
					"__typename": "PullRequestReview", "id": id, "author": map[string]string{"login": rv.Author},
					"editor": editor, "lastEditedAt": editedAt, "body": rv.Body, "state": rv.State, "commit": map[string]string{"oid": rv.CommitSHA},
					"pullRequest": map[string]any{"number": number, "repository": map[string]string{"nameWithOwner": testRepo}},
				}})
				return
			}
		}
		reply(map[string]any{"node": nil})
	case strings.Contains(req.Query, "enqueuePullRequest"):
		kind = "enqueue"
		id, _ := req.Variables["id"].(string)
		head, _ := req.Variables["head"].(string)
		f.enqueued = append(f.enqueued, id+"@"+head)
		n, _ := strconv.Atoi(strings.TrimPrefix(id, "PR_"))
		f.queued[n] = "QUEUED"
		reply(map[string]any{"enqueuePullRequest": map[string]any{"mergeQueueEntry": map[string]any{"state": "QUEUED"}}})
	case strings.Contains(req.Query, "mergeQueue(branch"):
		kind = "merge_queue"
		var nodes []map[string]any
		for _, e := range f.queue {
			n := map[string]any{"state": e.State, "headCommit": nil, "pullRequest": map[string]any{"number": e.Number, "headRefOid": e.PRHeadSHA}}
			if e.HeadCommit != "" {
				n["headCommit"] = map[string]any{"oid": e.HeadCommit}
			}
			nodes = append(nodes, n)
		}
		reply(map[string]any{"repository": map[string]any{"mergeQueue": map[string]any{"entries": map[string]any{"nodes": nodes}}}})
	case strings.Contains(req.Query, "timelineItems"):
		kind = "heads"
		pr := f.prs[number]
		appID, _ := req.Variables["appId"].(float64)
		check, _ := req.Variables["check"].(string)
		var nodes []map[string]any
		for _, h := range pr.Heads {
			suites := map[int64][]map[string]any{}
			for _, c := range f.checks {
				if c.SHA == h && c.AppID == int64(appID) && c.Name == check && c.Status == "completed" {
					suites[c.AppID] = append(suites[c.AppID], map[string]any{"name": c.Name, "conclusion": strings.ToUpper(c.Conclusion)})
				}
			}
			var sn []map[string]any
			for app, runs := range suites {
				sn = append(sn, map[string]any{"app": map[string]any{"databaseId": app}, "checkRuns": map[string]any{"nodes": runs}})
			}
			nodes = append(nodes, map[string]any{"__typename": "PullRequestCommit", "commit": map[string]any{
				"oid": h, "checkSuites": map[string]any{"nodes": sn},
			}})
		}
		reply(map[string]any{"repository": map[string]any{"pullRequest": map[string]any{"timelineItems": map[string]any{
			"pageInfo": map[string]any{"hasPreviousPage": pr.TimelineTruncated}, "nodes": nodes,
		}}}})
	case strings.Contains(req.Query, "mergeQueueEntry"):
		kind = "pull_request"
		pr := f.prs[number]
		head := pr.Head
		if h, ok := f.queueHead[number]; ok {
			head = h
		}
		var entry any
		if s := f.queued[number]; s != "" {
			entry = map[string]any{"state": s}
		}
		labels := []map[string]string{}
		for _, label := range pr.Labels {
			labels = append(labels, map[string]string{"name": label})
		}
		state := "OPEN"
		if pr.Closed {
			state = "CLOSED"
		}
		reply(map[string]any{"repository": map[string]any{"pullRequest": map[string]any{
			"id": fmt.Sprintf("PR_%d", number), "state": state, "isDraft": pr.Draft, "headRefOid": head,
			"baseRefName": pr.Base, "mergeQueueEntry": entry,
			"labels": map[string]any{"nodes": labels, "pageInfo": map[string]bool{"hasNextPage": false}},
		}}})
		if after := f.afterQueueRead; after != nil {
			f.afterQueueRead = nil
			after()
		}
	default:
		f.t.Errorf("unexpected GraphQL query %s", req.Query)
	}
}

type reviewerTokens struct{}

func (reviewerTokens) Token(context.Context, string) (string, error) { return "ghs_reviewer", nil }
func (reviewerTokens) Invalidate(string)                             {}

// ownComments are the comments the reviewer wrote on a PR.
func (f *fakeGitHub) ownComments(number int) []github.Comment {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []github.Comment
	for _, c := range f.prs[number].Comments {
		if c.Author == botLogin {
			out = append(out, c)
		}
	}
	return out
}

// fakeKagent models the gateway's handling of a task-opening message: a
// repeated message ID is answered with the task holding it, and rejected
// when its content differs. A message naming a context opens its task in
// that session, which must exist for the same agent.
type fakeKagent struct {
	mu         sync.Mutex
	tasks      map[string]*fakeTask
	initial    map[string]string // message ID -> task ID
	texts      map[string]string // message ID -> first text sent
	gone       map[string]bool   // context IDs whose sessions were deleted
	sends      []kagent.Message
	executions int
	cancels    []string
	// cancelErr fails cancellations, leaving the task running.
	cancelErr error
	rejected  int
	// sendErr fails the next sends before anything is dispatched.
	sendErr []error
	// loseResponse accepts the next send but loses its answer.
	loseResponse bool
	// afterSend runs after each send, outside the lock.
	afterSend func()
	// revision is the Agent's current revision, which a new session is
	// pinned to; revisions holds each session's.
	revision  string
	revisions map[string]string
	// listErr fails ListTasks.
	listErr error
	// goal is the reply a goal check's task finishes with as it opens;
	// empty leaves it working. onGoal, if set, runs as it opens.
	goal        string
	onGoal      func()
	goalReplies []string // Optional successive replies, then fall back to goal.
}

type fakeTask struct {
	task            kagent.Task
	agent           kagent.AgentRef
	cancelRequested bool
}

func newFakeKagent() *fakeKagent {
	return &fakeKagent{
		tasks: map[string]*fakeTask{}, initial: map[string]string{}, texts: map[string]string{}, gone: map[string]bool{},
		revision: "rev-1", revisions: map[string]string{},
		goal: goalReply(`{"criteria": [], "overall": "met"}`),
	}
}

// goalReply is a goal check's reply ending in a fenced answer.
func goalReply(answer string) string { return "I read the issue.\n\n```json\n" + answer + "\n```\n" }

// session reports the agent whose task opened a context, and whether one did.
func (k *fakeKagent) session(contextID string) (kagent.AgentRef, bool) {
	for _, t := range k.tasks {
		if t.task.ContextID == contextID {
			return t.agent, !k.gone[contextID]
		}
	}
	return kagent.AgentRef{}, false
}

func copyTask(t *kagent.Task) *kagent.Task {
	c := *t
	c.History = append([]*kagent.Message(nil), t.History...)
	return &c
}

func (k *fakeKagent) Send(_ context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error) {
	k.mu.Lock()
	hook := k.afterSend
	t, err := k.send(agent, msg)
	k.mu.Unlock()
	if hook != nil {
		hook()
	}
	return t, err
}

func (k *fakeKagent) send(agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error) {
	k.sends = append(k.sends, msg)
	if len(k.sendErr) > 0 {
		err := k.sendErr[0]
		k.sendErr = k.sendErr[1:]
		return nil, err
	}
	if msg.ContextID != "" {
		if owner, ok := k.session(msg.ContextID); !ok || owner != agent {
			return nil, fmt.Errorf("%w: context", kagent.ErrTaskNotFound)
		}
	}
	text := msg.Parts[0].Text
	if first, ok := k.texts[msg.MessageID]; ok && first != text {
		k.rejected++
		return nil, fmt.Errorf("%w: message ID reused with different content", kagent.ErrRejected)
	}
	k.texts[msg.MessageID] = text
	if id, ok := k.initial[msg.MessageID]; ok {
		return copyTask(&k.tasks[id].task), nil
	}
	id := fmt.Sprintf("task-%d", len(k.tasks)+1)
	m := msg
	m.Role, m.TaskID = "ROLE_USER", id
	if m.ContextID == "" {
		m.ContextID = "ctx-" + id
		k.revisions[m.ContextID] = k.revision
	}
	k.tasks[id] = &fakeTask{agent: agent, task: kagent.Task{
		ID: id, ContextID: m.ContextID, Status: kagent.TaskStatus{State: kagent.StateWorking}, History: []*kagent.Message{&m},
	}}
	k.initial[msg.MessageID] = id
	k.executions++
	if k.onGoal != nil && strings.Contains(msg.MessageID, "/goal/") {
		k.onGoal()
	}
	goal := k.goal
	if strings.Contains(msg.MessageID, "/goal/") && len(k.goalReplies) > 0 {
		goal, k.goalReplies = k.goalReplies[0], k.goalReplies[1:]
	}
	if goal != "" && strings.Contains(msg.MessageID, "/goal/") {
		t := &k.tasks[id].task
		reply := &kagent.Message{MessageID: id + "-reply", TaskID: id, Role: "ROLE_AGENT", Parts: []kagent.Part{{Text: goal}}}
		t.History, t.Status = append(t.History, reply), kagent.TaskStatus{State: kagent.StateCompleted, Message: reply}
	}
	if k.loseResponse {
		k.loseResponse = false
		return nil, fmt.Errorf("%w: response lost", kagent.ErrAmbiguous)
	}
	return copyTask(&k.tasks[id].task), nil
}

func (k *fakeKagent) GetTask(_ context.Context, agent kagent.AgentRef, id string) (*kagent.Task, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t, ok := k.tasks[id]
	if !ok || t.agent != agent {
		return nil, fmt.Errorf("%w: task", kagent.ErrTaskNotFound)
	}
	if t.cancelRequested && !kagent.Terminal(t.task.Status.State) {
		t.task.Status = kagent.TaskStatus{State: kagent.StateCanceled}
	}
	return copyTask(&t.task), nil
}

func (k *fakeKagent) CancelTask(_ context.Context, agent kagent.AgentRef, id string) (*kagent.Task, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.cancels = append(k.cancels, id)
	if k.cancelErr != nil {
		return nil, k.cancelErr
	}
	t, ok := k.tasks[id]
	if !ok || t.agent != agent {
		return nil, fmt.Errorf("%w: task", kagent.ErrTaskNotFound)
	}
	if kagent.Terminal(t.task.Status.State) {
		return nil, errors.New("task not cancelable")
	}
	t.cancelRequested = true
	return copyTask(&t.task), nil
}

// ListTasks lists every task of one session of the agent, without history,
// on one page.
func (k *fakeKagent) ListTasks(_ context.Context, agent kagent.AgentRef, contextID, state, pageToken string) (*kagent.TaskPage, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.listErr != nil {
		return nil, k.listErr
	}
	if contextID == "" || state != "" || pageToken != "" {
		return nil, fmt.Errorf("unexpected listing %q %q %q", contextID, state, pageToken)
	}
	out := []*kagent.Task{}
	for _, t := range k.tasks {
		if t.agent == agent && t.task.ContextID == contextID {
			c := t.task
			c.History, c.Artifacts = nil, nil
			out = append(out, &c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return &kagent.TaskPage{Tasks: out, TotalSize: len(out), PageSize: 100}, nil
}

// open starts a task in an existing session outside the reviewer, as an
// interrupted turn leaves one.
func (k *fakeKagent) open(contextID string) string {
	k.mu.Lock()
	defer k.mu.Unlock()
	agent, _ := k.session(contextID)
	id := fmt.Sprintf("task-%d", len(k.tasks)+1)
	k.tasks[id] = &fakeTask{agent: agent, task: kagent.Task{ID: id, ContextID: contextID, Status: kagent.TaskStatus{State: kagent.StateCanceled}}}
	return id
}

// finish ends a task with an agent reply.
func (k *fakeKagent) finish(id, state, text string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t := k.tasks[id]
	reply := &kagent.Message{MessageID: id + "-reply", TaskID: id, Role: "ROLE_AGENT", Parts: []kagent.Part{{Text: text}}}
	t.task.History = append(t.task.History, reply)
	t.task.Status = kagent.TaskStatus{State: state, Message: reply}
}

// pad adds an artifact of n bytes to a task, as a tool's output would.
func (k *fakeKagent) pad(id string, n int) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t := k.tasks[id]
	t.task.Artifacts = append(t.task.Artifacts, &kagent.Artifact{ArtifactID: id + "-tool", Parts: []kagent.Part{{Text: strings.Repeat("x", n)}}})
}

// stamp sets the task-lifetime usage kagent's ADK runtimes report.
func (k *fakeKagent) stamp(id string, usage map[string]any) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.tasks[id].task.Metadata = map[string]any{usageExtension: usage}
}

// finishOpen ends every task still working with the same reply.
func (k *fakeKagent) finishOpen(state, text string) {
	k.mu.Lock()
	var open []string
	for id, t := range k.tasks {
		if !kagent.Quiescent(t.task.Status.State) && !t.cancelRequested {
			open = append(open, id)
		}
	}
	k.mu.Unlock()
	for _, id := range open {
		k.finish(id, state, text)
	}
}

func (k *fakeKagent) rejections() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.rejected
}

func (k *fakeKagent) taskFor(msgID string) string {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.initial[msgID]
}

func (k *fakeKagent) sent() []kagent.Message {
	k.mu.Lock()
	defer k.mu.Unlock()
	return append([]kagent.Message(nil), k.sends...)
}

func (k *fakeKagent) state() (executions int, cancels []string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.executions, append([]string(nil), k.cancels...)
}

var testAgent = kagent.AgentRef{Namespace: "factory-review", Name: "reviewer"}

// fakeSessions answers GetSession for every context the fake kagent opened,
// as a READY session of the agent that opened it, unless a test sets
// another answer.
type fakeSessions struct {
	k          *fakeKagent
	mu         sync.Mutex
	set        map[string]*kagent.Session
	errs       map[string]error
	calls      []string
	agentErr   error
	agentReads int
}

func (s *fakeSessions) GetSession(_ context.Context, id string) (*kagent.Session, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, id)
	if err := s.errs[id]; err != nil {
		return nil, err
	}
	if sess := s.set[id]; sess != nil {
		c := *sess
		return &c, nil
	}
	s.k.mu.Lock()
	defer s.k.mu.Unlock()
	agent, ok := s.k.session(id)
	if !ok {
		return nil, fmt.Errorf("%w: %s", kagent.ErrSessionNotFound, id)
	}
	return &kagent.Session{
		ID: id, ContextID: id, State: kagent.RuntimeReady, AgentNamespace: agent.Namespace, AgentName: agent.Name,
		PreparedRevision: s.k.revisions[id],
	}, nil
}

// AgentRevision answers the fake kagent's current revision.
func (s *fakeSessions) AgentRevision(_ context.Context, agent kagent.AgentRef) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.agentReads++
	if s.agentErr != nil {
		return "", s.agentErr
	}
	if agent != testAgent {
		return "", fmt.Errorf("unexpected agent %v", agent)
	}
	s.k.mu.Lock()
	defer s.k.mu.Unlock()
	return s.k.revision, nil
}

func (s *fakeSessions) answer(id string, sess *kagent.Session, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if sess != nil {
		s.set[id] = sess
	}
	if err != nil {
		s.errs[id] = err
	}
}

func (s *fakeSessions) reads() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.calls...)
}

type harness struct {
	t               *testing.T
	gh              *fakeGitHub
	k               *fakeKagent
	sessions        *fakeSessions
	m               *metrics.Metrics
	log             *syncBuffer
	cfg             Config
	srv             *httptest.Server
	commentsClient  *github.BrokerComments
	startupRecovery bool
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func newHarness(t *testing.T) *harness {
	audit, err := OpenGoalAudit(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	gh, srv := newFakeGitHub(t)
	k := newFakeKagent()
	h := &harness{
		t: t, gh: gh, k: k, m: metrics.New(nil), log: &syncBuffer{}, srv: srv,
		sessions: &fakeSessions{k: k, set: map[string]*kagent.Session{}, errs: map[string]error{}},
		cfg: Config{
			GoalAudit: audit,
			CheckName: "factory review", AppID: testAppID, Concurrency: 2,
			TaskPoll: 2 * time.Millisecond, Reissue: 2 * time.Millisecond, Timeout: 10 * time.Second,
			RetryBackoff: 2 * time.Millisecond, MaxSends: 3, DispatchWindow: 5 * time.Millisecond,
			RuleIDs: []string{"nil-deref"},
			Bindings: []Binding{{
				Name: "homelab", Owner: "example-org", Repo: "homelab", BaseBranch: "main", Agent: testAgent,
				RequiredChecks:    []Check{{Name: "pr-checks", StatusCreator: ciCreator}},
				SkipLabels:        []string{"no-review"},
				EnqueueAuthors:    []string{"jomcgi"},
				HumanGatePaths:    []string{"REVIEW.md", "**/REVIEW.md", "projects/factory/reviewer/**", ".github/CODEOWNERS"},
				HumanGateLabels:   []string{"human", "needs-human"},
				HumanApprovers:    []string{"jomcgi"},
				TrustedCommenters: []string{"jomcgi"},
				ContextDocs:       []string{"AGENTS.md", "docs/agents/reference.md"},
				GeneratedPaths:    []string{"**/*.lock", "**/go.sum", "**/render.sha256", "**/BUILD", "**/charts/*.tgz"},
				MaxDiffLines:      500,
				MaxDiffFiles:      20,
			}},
		},
	}
	gh.now = func() time.Time {
		if h.cfg.Now != nil {
			return h.cfg.Now()
		}
		return time.Now()
	}
	return h
}

type commentProxyBearer struct{}

func (commentProxyBearer) Token() (string, error) { return "test-service-account", nil }

// comments exercises the deployed broker client and proxy against fake GitHub.
func (h *harness) comments() *github.BrokerComments {
	if h.commentsClient != nil {
		return h.commentsClient
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		h.t.Fatal(err)
	}
	proxy := &brokergithub.CommentProxy{
		Author: botLogin,
		Minter: &brokergithub.Minter{
			App:                  &brokergithub.App{APIURL: h.srv.URL, AppID: "1", InstallationID: "1", Key: key, Client: h.srv.Client()},
			Bindings:             []brokerconfig.Binding{{Name: "homelab", Repository: testRepo}},
			ConnectorPermissions: map[string]string{"issues": "write", "pull_requests": "write"},
			Metrics:              brokermetrics.New(),
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != github.DefaultCommentsPath || r.Header.Get("Authorization") != "Bearer test-service-account" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		proxy.ServeHTTP(w, r)
	}))
	h.t.Cleanup(srv.Close)
	h.commentsClient = &github.BrokerComments{URL: srv.URL, Prefix: CommentMarker, Bearer: commentProxyBearer{}, HTTP: srv.Client()}
	return h.commentsClient
}

// engine builds a fresh engine, as a new process would, sharing the fakes.
// With a CommentAuthor and no Comments, it writes comments through the broker.
func (h *harness) engine() *Engine {
	gh := &github.Client{BaseURL: h.srv.URL, Tokens: reviewerTokens{}, ETags: &github.ETagCache{}}
	log := slog.New(slog.NewJSONHandler(h.log, &slog.HandlerOptions{Level: slog.LevelDebug}))
	cfg := h.cfg
	if cfg.Sessions == nil && h.sessions != nil {
		cfg.Sessions = h.sessions
	}
	if cfg.CommentAuthor != "" && cfg.Comments == nil {
		cfg.Comments = h.comments()
	}
	e := New(cfg, gh, h.k, h.m, log)
	if !h.startupRecovery { // Most cases isolate steady-state behavior; recovery cases exercise the startup scan.
		e.epoch = "steady-state-test"
		for i := range e.cfg.Bindings {
			e.cfg.Bindings[i].startupBefore = &time.Time{}
		}
	}
	return e
}

// start runs one engine until the test ends or stop is called.
func (h *harness) start() (*Engine, context.Context, func()) {
	e := h.engine()
	ctx, cancel := context.WithCancel(context.Background())
	stop := func() {
		cancel()
		e.Wait()
	}
	h.t.Cleanup(stop)
	return e, ctx, stop
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// msgID is the message ID of an attempt of the review the head's latest
// review check holds, or empty before it exists.
func (h *harness) msgID(number int, sha string, attempt int) string {
	c := h.gh.check(sha)
	if c == nil {
		return ""
	}
	return AttemptMessageID(testRepo, number, sha, c.ID, attempt)
}

// taskOn is the task of an attempt of the head's latest review, or empty:
// the continuation of the previous head's session, else a fresh one.
func (h *harness) taskOn(number int, sha string, attempt int) string {
	if m := h.msgID(number, sha, attempt); m != "" {
		if id := h.k.taskFor(m + "/resume"); id != "" {
			return id
		}
		return h.k.taskFor(m)
	}
	return ""
}

// report renders an agent reply ending in a fenced findings report.
func report(t *testing.T, v any) string {
	t.Helper()
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return "I reviewed the change.\n\n```json\n" + string(b) + "\n```\n"
}
