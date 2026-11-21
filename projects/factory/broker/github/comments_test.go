// invariant: FACT-13
package github

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// fakeComments fakes the conversation comment endpoints on
// example-org/homelab: PR 7 and issue 9 exist, comment 100 on PR 7 is the
// bot's, comment 101 on PR 7 is a person's, comment 102 on issue 9 is the
// bot's.
type fakeComments struct {
	mu       sync.Mutex
	comments map[int64]ghCommentFixture
	next     int64
	calls    []string
	tokens   []string
	endless  map[int]bool // always name a next page
}

type ghCommentFixture struct {
	number int
	pr     bool
	login  string
	body   string
}

const (
	bot    = "jomcgi-factory-review[bot]"
	marker = "<!-- factory-review -->"
)

func newCommentHarness(t *testing.T) (*harness, *fakeComments, *CommentProxy) {
	t.Helper()
	h := newHarness(t)
	f := &fakeComments{next: 200, endless: map[int]bool{}, comments: map[int64]ghCommentFixture{
		100: {number: 7, pr: true, login: bot, body: "<!-- factory-review -->\nold"},
		101: {number: 7, pr: true, login: "someone", body: "a person"},
		102: {number: 9, pr: false, login: bot, body: "<!-- factory-review -->\non an issue"},
	}}
	mint := h.gh.Config.Handler
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/repos/") {
			mint.ServeHTTP(w, r)
			return
		}
		f.serve(t, w, r)
	}))
	t.Cleanup(srv.Close)
	h.minter.App.APIURL = srv.URL
	h.minter.App.Client = srv.Client()
	h.minter.ConnectorPermissions = map[string]string{"issues": "write", "pull_requests": "write"}
	return h, f, &CommentProxy{Minter: h.minter, Author: bot}
}

func (f *fakeComments) serve(t *testing.T, w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	f.tokens = append(f.tokens, r.Header.Get("Authorization"))
	path := strings.TrimPrefix(r.URL.Path, "/repos/example-org/homelab")
	if path == r.URL.Path {
		http.NotFound(w, r)
		return
	}
	render := func(id int64, c ghCommentFixture) map[string]any {
		kind := "issues"
		if c.pr {
			kind = "pull"
		}
		return map[string]any{
			"id": id, "body": c.body, "user": map[string]string{"login": c.login},
			"html_url": fmt.Sprintf("https://github.com/example-org/homelab/%s/%d#issuecomment-%d", kind, c.number, id),
		}
	}
	switch {
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/pulls/"):
		if path != "/pulls/7" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]int{"number": 7})
	case r.Method == http.MethodGet && strings.HasPrefix(path, "/issues/comments/"):
		id, _ := strconv.ParseInt(strings.TrimPrefix(path, "/issues/comments/"), 10, 64)
		c, ok := f.comments[id]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(render(id, c))
	case r.Method == http.MethodPatch && strings.HasPrefix(path, "/issues/comments/"):
		id, _ := strconv.ParseInt(strings.TrimPrefix(path, "/issues/comments/"), 10, 64)
		var in struct{ Body string }
		_ = json.NewDecoder(r.Body).Decode(&in)
		c := f.comments[id]
		c.body = in.Body
		f.comments[id] = c
		_ = json.NewEncoder(w).Encode(render(id, c))
	case r.Method == http.MethodGet && path == "/issues/2/comments":
		// A page cut off after a whole comment, before its closing bracket.
		_, _ = w.Write([]byte(`[{"id":170,"body":"x","html_url":"https://github.com/example-org/homelab/pull/2","user":{"login":"someone"}}`))
	case r.Method == http.MethodGet && strings.HasSuffix(path, "/comments"):
		number, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		var all []map[string]any
		for id := int64(100); id < f.next; id++ {
			if c, ok := f.comments[id]; ok && c.number == number {
				all = append(all, render(id, c))
			}
		}
		per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		lo, hi := min((page-1)*per, len(all)), min(page*per, len(all))
		if hi < len(all) || f.endless[number] {
			w.Header().Set("Link", `<https://api.github.com/x?page=next>; rel="next"`)
		}
		out := all[lo:hi]
		if out == nil {
			out = []map[string]any{}
		}
		_ = json.NewEncoder(w).Encode(out)
	case r.Method == http.MethodPost && strings.HasSuffix(path, "/comments"):
		number, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		var in struct{ Body string }
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &in)
		id := f.next
		f.next++
		f.comments[id] = ghCommentFixture{number: number, pr: true, login: bot, body: in.Body}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(render(id, f.comments[id]))
	default:
		t.Errorf("unexpected GitHub call %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusTeapot)
	}
}

func (f *fakeComments) writes() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, c := range f.calls {
		if !strings.HasPrefix(c, "GET ") {
			out = append(out, c)
		}
	}
	return out
}

func doProxy(p *CommentProxy, method, target, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	var rd io.Reader
	if body != "" {
		rd = strings.NewReader(body)
	}
	p.ServeHTTP(rec, httptest.NewRequest(method, target, rd))
	return rec
}

func findComment(t *testing.T, p *CommentProxy, number int) (*ProxiedComment, bool) {
	t.Helper()
	rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/homelab&prefix="+url.QueryEscape(marker)+"&number="+strconv.Itoa(number), "")
	var out struct {
		Comment  *ProxiedComment `json:"comment"`
		Complete bool            `json:"complete"`
	}
	if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" || json.Unmarshal(rec.Body.Bytes(), &out) != nil {
		t.Fatalf("find %d = %d %.200s", number, rec.Code, rec.Body)
	}
	if strings.Contains(rec.Body.String(), "ghs_") {
		t.Fatalf("the proxy answer carries a token: %.200s", rec.Body)
	}
	return out.Comment, out.Complete
}

func (f *fakeComments) listCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, "GET ") && strings.HasSuffix(c, "/comments") {
			n++
		}
	}
	return n
}

func TestCommentProxyFindsTheBotsOldestComment(t *testing.T) {
	h, f, p := newCommentHarness(t)
	got, complete := findComment(t, p, 7)
	if got == nil || !complete || got.ID != 100 || got.Author != bot || f.listCalls() != 1 {
		t.Fatalf("find = %+v, %v after %d pages", got, complete, f.listCalls())
	}
	// The search used the comment token, minted for that repository with
	// the comment permissions.
	req := h.gh.lastRequests()[0]
	if len(req.Repositories) != 1 || req.Repositories[0] != "homelab" || req.Permissions["pull_requests"] != "write" ||
		f.tokens[0] != "Bearer ghs_fake1" {
		t.Fatalf("mint = %+v, token %q", req, f.tokens[0])
	}
	// A PR with no bot comment and no more pages: none, and complete.
	f.mu.Lock()
	f.comments[150] = ghCommentFixture{number: 5, pr: true, login: "someone", body: "x"}
	f.mu.Unlock()
	if got, complete := findComment(t, p, 5); got != nil || !complete {
		t.Fatalf("no bot comment = %+v, %v", got, complete)
	}
	for _, target := range []string{
		"/github/reviewer-comments?repository=example-org/homelab&prefix=m&number=0",
		"/github/reviewer-comments?repository=example-org/homelab&prefix=m&number=x",
		"/github/reviewer-comments?repository=example-org/homelab&number=7",
		"/github/reviewer-comments?repository=example-org/homelab&number=7&prefix=" + strings.Repeat("m", 257),
	} {
		if rec := doProxy(p, http.MethodGet, target, ""); rec.Code != http.StatusBadRequest {
			t.Fatalf("%s = %d", target, rec.Code)
		}
	}
	if rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/other&prefix=m&number=7", ""); rec.Code != http.StatusNotFound ||
		!strings.Contains(rec.Body.String(), "repository_not_bound") {
		t.Fatalf("unbound find = %d %s", rec.Code, rec.Body)
	}
}

// The search reads pages until the first one holding the bot's comment, and
// at most FindPages; past that bound, nothing found is not complete.
func TestCommentProxyStopsAtTheFirstMatchAndTheBound(t *testing.T) {
	_, f, p := newCommentHarness(t)
	f.mu.Lock()
	for i := range 150 {
		f.comments[int64(300+i)] = ghCommentFixture{number: 6, pr: true, login: "someone", body: "x"}
	}
	f.comments[450] = ghCommentFixture{number: 6, pr: true, login: bot, body: "<!-- factory-review -->\nmine"}
	for i := range 300 {
		f.comments[int64(451+i)] = ghCommentFixture{number: 6, pr: true, login: "someone", body: "x"}
	}
	f.comments[800] = ghCommentFixture{number: 4, pr: true, login: "someone", body: "x"}
	f.endless[4] = true
	f.next = 801
	f.mu.Unlock()
	got, complete := findComment(t, p, 6)
	if got == nil || got.ID != 450 || !complete || f.listCalls() != 2 {
		t.Fatalf("find on page 2 = %+v, %v after %d pages", got, complete, f.listCalls())
	}
	got, complete = findComment(t, p, 4)
	if got != nil || complete || f.listCalls() != 2+FindPages {
		t.Fatalf("find at the bound = %+v, %v after %d pages", got, complete, f.listCalls())
	}
}

func TestCommentProxyDiscardsUnmatchedBodies(t *testing.T) {
	for _, existing := range []bool{false, true} {
		t.Run(fmt.Sprintf("existing=%v", existing), func(t *testing.T) {
			// Retaining these pages would keep roughly 180 MiB of bodies.
			// Streaming them must keep the heap increase below 64 MiB.
			const pages = 30
			h, _, p := newCommentHarness(t)
			body := strings.Repeat("x", 60<<10)
			heap := make(chan uint64, 1)
			mint := h.gh.Config.Handler
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !strings.HasPrefix(r.URL.Path, "/repos/") {
					mint.ServeHTTP(w, r)
					return
				}
				page, _ := strconv.Atoi(r.URL.Query().Get("page"))
				if page == pages {
					runtime.GC()
					var stats runtime.MemStats
					runtime.ReadMemStats(&stats)
					heap <- stats.HeapAlloc
				} else {
					w.Header().Set("Link", fmt.Sprintf(`</comments?page=%d>; rel="next"`, page+1))
				}
				_, _ = fmt.Fprint(w, "[")
				for i := range 100 {
					if i > 0 {
						_, _ = fmt.Fprint(w, ",")
					}
					login, text := "someone", body
					if existing && page == pages && i == 99 {
						login, text = bot, marker+"\nmatch"
					}
					_, _ = fmt.Fprintf(w, `{"id":%d,"body":%q,"user":{"login":%q}}`, (page-1)*100+i+1, text, login)
				}
				_, _ = fmt.Fprint(w, "]")
			}))
			t.Cleanup(srv.Close)
			h.minter.App.APIURL = srv.URL
			h.minter.App.Client = srv.Client()
			runtime.GC()
			var before runtime.MemStats
			runtime.ReadMemStats(&before)
			comment, complete := findComment(t, p, 7)
			if !complete || (comment != nil) != existing {
				t.Fatalf("found %v, complete %v", comment != nil, complete)
			}
			if existing && (comment.ID != pages*100 || comment.Body != marker+"\nmatch") {
				t.Fatal("lost the matching comment after discarding earlier pages")
			}
			if live := <-heap; live > before.HeapAlloc+(64<<20) {
				t.Fatalf("lookup retained %d bytes above baseline, want at most 64 MiB", live-before.HeapAlloc)
			}
		})
	}
}

func TestCommentProxyCreatesOnlyOnPullRequests(t *testing.T) {
	_, f, p := newCommentHarness(t)
	rec := doProxy(p, http.MethodPost, "/github/reviewer-comments", `{"repository":"example-org/homelab","number":7,"body":"<!-- factory-review -->\nnew"}`)
	var got ProxiedComment
	_ = json.Unmarshal(rec.Body.Bytes(), &got)
	if rec.Code != http.StatusCreated || got.ID != 200 || got.Author != bot || got.Body != "<!-- factory-review -->\nnew" {
		t.Fatalf("create = %d %s", rec.Code, rec.Body)
	}
	for _, tc := range []struct {
		body   string
		status int
		code   string
	}{
		{`{"repository":"example-org/homelab","number":9,"body":"on an issue"}`, http.StatusUnprocessableEntity, "not_a_pull_request"},
		{`{"repository":"example-org/other","number":7,"body":"x"}`, http.StatusNotFound, "repository_not_bound"},
		{`{"repository":"example-org/homelab","number":7,"body":"   "}`, http.StatusBadRequest, "bad_request"},
		{`{"repository":"example-org/homelab","number":7,"body":"` + strings.Repeat("a", MaxCommentBody+1) + `"}`, http.StatusBadRequest, "bad_request"},
		{`{"repository":"example-org/homelab","number":7,"body":"x","event":"APPROVE"}`, http.StatusBadRequest, "bad_request"},
	} {
		rec := doProxy(p, http.MethodPost, "/github/reviewer-comments", tc.body)
		if rec.Code != tc.status || !strings.Contains(rec.Body.String(), tc.code) {
			t.Fatalf("create %.80s = %d %s, want %d %s", tc.body, rec.Code, rec.Body, tc.status, tc.code)
		}
	}
	if w := f.writes(); len(w) != 1 || w[0] != "POST /repos/example-org/homelab/issues/7/comments" {
		t.Fatalf("GitHub writes = %v", w)
	}
}

func TestCommentProxyEditsOnlyTheBotsPRComments(t *testing.T) {
	_, f, p := newCommentHarness(t)
	rec := doProxy(p, http.MethodPatch, "/github/reviewer-comments", `{"repository":"example-org/homelab","id":100,"body":"<!-- factory-review -->\nnew"}`)
	var got ProxiedComment
	_ = json.Unmarshal(rec.Body.Bytes(), &got)
	if rec.Code != http.StatusOK || got.ID != 100 || got.Body != "<!-- factory-review -->\nnew" {
		t.Fatalf("update = %d %s", rec.Code, rec.Body)
	}
	for _, tc := range []struct {
		body   string
		status int
		code   string
	}{
		{`{"repository":"example-org/homelab","id":101,"body":"x"}`, http.StatusForbidden, "not_the_reviewers_comment"},
		{`{"repository":"example-org/homelab","id":102,"body":"x"}`, http.StatusForbidden, "not_the_reviewers_comment"},
		{`{"repository":"example-org/homelab","id":999,"body":"x"}`, http.StatusNotFound, "not_found"},
		{`{"repository":"example-org/homelab","id":0,"body":"x"}`, http.StatusBadRequest, "bad_request"},
	} {
		rec := doProxy(p, http.MethodPatch, "/github/reviewer-comments", tc.body)
		if rec.Code != tc.status || !strings.Contains(rec.Body.String(), tc.code) {
			t.Fatalf("update %s = %d %s, want %d %s", tc.body, rec.Code, rec.Body, tc.status, tc.code)
		}
	}
	if w := f.writes(); len(w) != 1 || w[0] != "PATCH /repos/example-org/homelab/issues/comments/100" {
		t.Fatalf("GitHub writes = %v", w)
	}
}

func TestCommentProxyServesNothingElse(t *testing.T) {
	_, f, p := newCommentHarness(t)
	for _, m := range []string{http.MethodPut, http.MethodDelete} {
		if rec := doProxy(p, m, "/github/reviewer-comments", `{}`); rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s = %d", m, rec.Code)
		}
	}
	if len(f.writes()) != 0 {
		t.Fatalf("GitHub writes = %v", f.writes())
	}
}

// Escaping can make a valid body six times longer on the wire: a create or
// edit near the size limit, and a search over a page of the largest
// comments, still go through.
func TestCommentProxyHandlesEscapedNearLimitComments(t *testing.T) {
	_, f, p := newCommentHarness(t)
	big := strings.Repeat("&", 60000)
	req, _ := json.Marshal(map[string]any{"repository": "example-org/homelab", "number": 7, "body": big})
	if len(req) < 6*60000 {
		t.Fatalf("request is %d bytes; the test needs an escaped body", len(req))
	}
	if rec := doProxy(p, http.MethodPost, "/github/reviewer-comments", string(req)); rec.Code != http.StatusCreated {
		t.Fatalf("escaped create = %d %.200s", rec.Code, rec.Body)
	}
	req, _ = json.Marshal(map[string]any{"repository": "example-org/homelab", "id": 100, "body": big})
	if rec := doProxy(p, http.MethodPatch, "/github/reviewer-comments", string(req)); rec.Code != http.StatusOK {
		t.Fatalf("escaped update = %d %.200s", rec.Code, rec.Body)
	}
	// A page larger than any whole-answer bound is searched a comment at a
	// time: 30 comments of 65536 characters, escaped to six bytes each, is
	// about 11 MiB, and the bot's comment comes last.
	f.mu.Lock()
	for i := range 30 {
		f.comments[int64(300+i)] = ghCommentFixture{number: 8, pr: true, login: "someone", body: strings.Repeat("&", MaxCommentBody)}
	}
	f.comments[330] = ghCommentFixture{number: 8, pr: true, login: bot, body: marker + "\n" + strings.Repeat("&", MaxCommentBody-len(marker)-1)}
	f.next = 331
	f.mu.Unlock()
	got, complete := findComment(t, p, 8)
	if got == nil || got.ID != 330 || !complete || len(got.Body) != MaxCommentBody {
		t.Fatalf("find on a large page = %v, %v", got != nil, complete)
	}
}

// The search matches the bot login and the marker: an older bot comment
// without the marker is passed over, and a page cut off before its closing
// bracket is an error, never a complete search.
func TestCommentProxyMatchesTheMarkerAndRejectsTruncatedPages(t *testing.T) {
	_, f, p := newCommentHarness(t)
	f.mu.Lock()
	f.comments[160] = ghCommentFixture{number: 3, pr: true, login: bot, body: "a note without the marker"}
	f.comments[161] = ghCommentFixture{number: 3, pr: true, login: bot, body: marker + "\nthe review"}
	f.mu.Unlock()
	if got, complete := findComment(t, p, 3); got == nil || got.ID != 161 || !complete {
		t.Fatalf("find past an unmarked bot comment = %+v, %v", got, complete)
	}
	rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/homelab&prefix="+url.QueryEscape(marker)+"&number=2", "")
	if rec.Code != http.StatusBadGateway || !strings.Contains(rec.Body.String(), "github_comment_failed") {
		t.Fatalf("truncated page = %d %s", rec.Code, rec.Body)
	}
}

func TestInlineReviewLifecycle(t *testing.T) {
	h, _, p := newCommentHarness(t)
	head := strings.Repeat("a", 40)
	var nodes []map[string]any
	wantComments := 25
	var posted, resolved int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/graphql":
			var in struct {
				Query     string
				Variables map[string]any
			}
			if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
				t.Error(err)
			}
			if strings.HasPrefix(in.Query, "mutation") {
				if in.Variables["id"] != "old" || in.Variables["body"] != "Cleared on "+head+"." {
					t.Error(in.Variables)
				}
				resolved++
				fmt.Fprint(w, `{"data":{}}`)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"repository": map[string]any{"pullRequest": map[string]any{"headRefOid": head, "reviewThreads": map[string]any{"nodes": nodes}}}}})
		case "/repos/example-org/homelab/pulls/7/reviews":
			var in struct {
				Event    string
				Comments []map[string]any
			}
			_ = json.NewDecoder(r.Body).Decode(&in)
			if r.Method != "POST" || in.Event != "COMMENT" || len(in.Comments) != wantComments {
				t.Errorf("unexpected review %s: %d", in.Event, len(in.Comments))
			}
			for _, c := range in.Comments {
				posted++
				nodes = append(nodes, testThread(fmt.Sprint(posted), bot, c["body"].(string), head))
			}
			fmt.Fprint(w, `{"id":42}`)
		default:
			if !strings.HasPrefix(r.URL.Path, "/app/") {
				t.Errorf("unexpected operation %s", r.URL.Path)
			}
			h.gh.Config.Handler.ServeHTTP(w, r)
		}
	}))
	defer srv.Close()
	h.minter.App.APIURL = srv.URL
	nodes = append(nodes, testThread("old", bot, inlinePrefix+strings.Repeat("0", 64)+" -->\nold", "prior"), testThread("someone", "human", inlinePrefix+strings.Repeat("c", 64)+" -->\nother", "prior"))
	in := map[string]any{"repository": "example-org/homelab", "number": 7, "head": head}
	var items []map[string]any
	var active []string
	for i := 1; i <= 26; i++ {
		key := fmt.Sprintf("%064x", i)
		active = append(active, key)
		items = append(items, map[string]any{"key": key, "path": "a", "start": 1, "end": 1, "body": "finding"})
	}
	in["fixed"] = []string{strings.Repeat("0", 64)}
	nodes = append(nodes, testThread("unaccounted", bot, inlinePrefix+strings.Repeat("d", 64)+" -->\nopen", "prior"))
	in["findings"], in["active"] = items, active
	send := func() {
		body, _ := json.Marshal(in)
		w := doProxy(p, "POST", "/?inline=1", string(body))
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	send()
	if posted != 25 || resolved != 1 {
		t.Fatal(posted, resolved)
	}
	nodes[0]["isResolved"] = true
	// On a later head the existing 25 persist without another notification.
	head = strings.Repeat("b", 40)
	in["head"] = head
	in["findings"] = items[:25]
	send()
	if posted != 25 || resolved != 1 {
		t.Fatal(posted, resolved)
	}
	// The first key returns on a new line after its earlier thread was cleared.
	key := strings.Repeat("0", 64)
	in["active"] = append(active, key)
	in["fixed"] = nil
	in["findings"] = []map[string]any{{"key": key, "path": "new.go", "start": 3, "end": 3, "body": "new occurrence"}}
	wantComments = 1
	send()
	send()
	if posted != 26 || resolved != 1 {
		t.Fatal(posted, resolved)
	}
}

func testThread(id, author, body, head string) map[string]any {
	return map[string]any{"id": id, "comments": map[string]any{"nodes": []map[string]any{{"url": "https://github.com/example-org/homelab/pull/7#discussion_r123", "body": body, "author": map[string]string{"login": author}, "originalCommit": map[string]string{"oid": head}}}}}
}
