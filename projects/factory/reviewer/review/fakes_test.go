package review

import (
	"bytes"
	"context"
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

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
)

const (
	testRepo  = "example-org/homelab"
	testAppID = 5758326
	ciCreator = "buildbuddy-io[bot]"
)

type fakePR struct {
	Number  int
	Title   string
	Body    string
	Author  string
	Head    string
	Base    string
	Draft   bool
	Labels  []string
	Updated time.Time
	Files   []github.PullRequestFile
	// Heads is the timeline's head history, oldest first; the current head
	// is appended when it changes.
	Heads    []string
	Reviews  []github.PullRequestReview
	Comments []github.Comment
	// TimelineTruncated reports older timeline events than GitHub lists.
	TimelineTruncated bool
	Closed            bool
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
	queue     []github.MergeQueueEntry
	queued    map[int]string // PR number -> merge queue state
	// queueHead overrides the head the GraphQL PR read reports.
	queueHead   map[int]string
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
	// fail answers "METHOD /path" with a status code; failGQL fails a
	// GraphQL call by kind.
	fail    map[string]int
	failGQL map[string]bool
	nextID  int64
	// closeAfterPage closes PR [1] once the open PR listing's page [0] is
	// served, as a PR that closes between two page reads would.
	closeAfterPage [2]int
}

func newFakeGitHub(t *testing.T) (*fakeGitHub, *httptest.Server) {
	f := &fakeGitHub{
		t: t, prs: map[int]*fakePR{}, statuses: map[string][]map[string]any{}, baseHead: "base-1",
		baseFiles: map[string]string{}, queued: map[int]string{}, queueHead: map[int]string{},
		fail: map[string]int{}, failGQL: map[string]bool{},
	}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, srv
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
	if len(pr.Heads) == 0 {
		pr.Heads = []string{pr.Head}
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
	pr.Reviews = append(pr.Reviews, github.PullRequestReview{Author: author, State: state, CommitSHA: sha, Body: body})
}

func (f *fakeGitHub) label(number int, labels ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.prs[number].Labels = labels
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
	if r.Header.Get("Authorization") != "Bearer ghs_reviewer" {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	if code := f.fail[r.Method+" "+r.URL.Path]; code != 0 {
		w.WriteHeader(code)
		_, _ = w.Write([]byte(`{"message":"injected failure"}`))
		return
	}
	prefix := "/repos/" + testRepo
	path := strings.TrimPrefix(r.URL.Path, prefix)
	q := r.URL.Query()
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/graphql":
		f.graphql(w, r)
	case r.Method == http.MethodGet && path == "/pulls":
		out := []map[string]any{}
		for _, pr := range f.sortedPRs() {
			if q.Get("state") != "open" || pr.Closed || pr.Base != q.Get("base") {
				continue
			}
			labels := []map[string]string{}
			for _, l := range pr.Labels {
				labels = append(labels, map[string]string{"name": l})
			}
			out = append(out, map[string]any{
				"number": pr.Number, "node_id": fmt.Sprintf("PR_%d", pr.Number), "state": "open", "draft": pr.Draft,
				"title": pr.Title, "body": pr.Body, "user": map[string]string{"login": pr.Author}, "labels": labels,
				"html_url":   fmt.Sprintf("https://github.com/%s/pull/%d", testRepo, pr.Number),
				"updated_at": pr.Updated.UTC().Format(time.RFC3339),
				"head":       map[string]string{"ref": fmt.Sprintf("branch-%d", pr.Number), "sha": pr.Head},
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
			out = append(out, map[string]any{"user": map[string]string{"login": rv.Author}, "state": rv.State, "commit_id": rv.CommitSHA, "body": rv.Body})
		}
		f.get(w, r, page(out, q))
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/issues/") && strings.HasSuffix(path, "/comments"):
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		out := []map[string]any{}
		for _, c := range f.prs[n].Comments {
			out = append(out, map[string]any{"user": map[string]string{"login": c.Author}, "body": c.Body})
		}
		f.get(w, r, out)
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/compare/"):
		base, head, _ := strings.Cut(strings.TrimPrefix(path, "/compare/"), "...")
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
		"output": map[string]any{"title": c.Title, "summary": c.Summary, "text": c.Text},
	}
	if c.Conclusion != "" {
		m["conclusion"] = c.Conclusion
	}
	if !c.StartedAt.IsZero() {
		m["started_at"] = c.StartedAt.UTC().Format(time.RFC3339Nano)
	}
	return m
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
func page(all []map[string]any, q map[string][]string) []map[string]any {
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
			"enqueue": "enqueuePullRequest", "heads": "timelineItems", "pull_request": "mergeQueueEntry {", "merge_queue": "mergeQueue(branch",
		}[k]) {
			kind = k
			_ = json.NewEncoder(w).Encode(map[string]any{"errors": []map[string]string{{"type": "FORBIDDEN", "message": "injected failure"}}})
			return
		}
	}
	switch {
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
		reply(map[string]any{"repository": map[string]any{"pullRequest": map[string]any{
			"id": fmt.Sprintf("PR_%d", number), "state": "OPEN", "isDraft": pr.Draft, "headRefOid": head,
			"baseRefName": pr.Base, "mergeQueueEntry": entry,
		}}})
	default:
		f.t.Errorf("unexpected GraphQL query %s", req.Query)
	}
}

type reviewerTokens struct{}

func (reviewerTokens) Token(context.Context, string) (string, error) { return "ghs_reviewer", nil }
func (reviewerTokens) Invalidate(string)                             {}

// fakeKagent models the gateway's handling of a task-opening message: a
// repeated message ID is answered with the task holding it, and rejected
// when its content differs.
type fakeKagent struct {
	mu         sync.Mutex
	tasks      map[string]*fakeTask
	initial    map[string]string // message ID -> task ID
	texts      map[string]string // message ID -> first text sent
	sends      []kagent.Message
	executions int
	cancels    []string
	rejected   int
	// sendErr fails the next sends before anything is dispatched.
	sendErr []error
	// loseResponse accepts the next send but loses its answer.
	loseResponse bool
	// afterSend runs after each send, outside the lock.
	afterSend func()
}

type fakeTask struct {
	task            kagent.Task
	agent           kagent.AgentRef
	cancelRequested bool
}

func newFakeKagent() *fakeKagent {
	return &fakeKagent{tasks: map[string]*fakeTask{}, initial: map[string]string{}, texts: map[string]string{}}
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
	m.Role, m.TaskID, m.ContextID = "ROLE_USER", id, "ctx-"+id
	k.tasks[id] = &fakeTask{agent: agent, task: kagent.Task{
		ID: id, ContextID: m.ContextID, Status: kagent.TaskStatus{State: kagent.StateWorking}, History: []*kagent.Message{&m},
	}}
	k.initial[msg.MessageID] = id
	k.executions++
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

// finish ends a task with an agent reply.
func (k *fakeKagent) finish(id, state, text string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t := k.tasks[id]
	reply := &kagent.Message{MessageID: id + "-reply", TaskID: id, Role: "ROLE_AGENT", Parts: []kagent.Part{{Text: text}}}
	t.task.History = append(t.task.History, reply)
	t.task.Status = kagent.TaskStatus{State: state, Message: reply}
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

type harness struct {
	t   *testing.T
	gh  *fakeGitHub
	k   *fakeKagent
	m   *metrics.Metrics
	log *syncBuffer
	cfg Config
	srv *httptest.Server
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
	gh, srv := newFakeGitHub(t)
	return &harness{
		t: t, gh: gh, k: newFakeKagent(), m: metrics.New(nil), log: &syncBuffer{}, srv: srv,
		cfg: Config{
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
}

// engine builds a fresh engine, as a new process would, sharing the fakes.
func (h *harness) engine() *Engine {
	gh := &github.Client{BaseURL: h.srv.URL, Tokens: reviewerTokens{}, ETags: &github.ETagCache{}}
	log := slog.New(slog.NewJSONHandler(h.log, &slog.HandlerOptions{Level: slog.LevelDebug}))
	return New(h.cfg, gh, h.k, h.m, log)
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

// taskOn is the task of an attempt of the head's latest review, or empty.
func (h *harness) taskOn(number int, sha string, attempt int) string {
	if m := h.msgID(number, sha, attempt); m != "" {
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
