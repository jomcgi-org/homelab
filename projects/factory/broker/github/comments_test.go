package github

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
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
}

type ghCommentFixture struct {
	number int
	pr     bool
	login  string
	body   string
}

const bot = "jomcgi-factory-review[bot]"

func newCommentHarness(t *testing.T) (*harness, *fakeComments, *CommentProxy) {
	t.Helper()
	h := newHarness(t)
	f := &fakeComments{next: 200, comments: map[int64]ghCommentFixture{
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
	case r.Method == http.MethodGet && strings.HasSuffix(path, "/comments"):
		number, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(path, "/issues/"), "/comments"))
		var out []map[string]any
		for id := int64(100); id < f.next; id++ {
			if c, ok := f.comments[id]; ok && c.number == number {
				out = append(out, render(id, c))
			}
		}
		if r.URL.Query().Get("page") == "1" && number == 7 {
			w.Header().Set("Link", `<https://api.github.com/x?page=2>; rel="next"`)
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

func TestCommentProxyListsAPage(t *testing.T) {
	h, f, p := newCommentHarness(t)
	rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/homelab&number=7&page=1", "")
	var out struct {
		Comments []ProxiedComment `json:"comments"`
		Next     bool             `json:"next"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" || !out.Next ||
		len(out.Comments) != 2 || out.Comments[0].ID != 100 || out.Comments[0].Author != bot {
		t.Fatalf("list = %d %s", rec.Code, rec.Body)
	}
	if strings.Contains(rec.Body.String(), "ghs_") {
		t.Fatalf("the proxy answer carries a token: %s", rec.Body)
	}
	// The listing used the comment token, minted for that repository with
	// the comment permissions.
	req := h.gh.lastRequests()[0]
	if len(req.Repositories) != 1 || req.Repositories[0] != "homelab" || req.Permissions["pull_requests"] != "write" ||
		f.tokens[0] != "Bearer ghs_fake1" {
		t.Fatalf("mint = %+v, token %q", req, f.tokens[0])
	}
	for _, target := range []string{
		"/github/reviewer-comments?repository=example-org/homelab&number=0&page=1",
		"/github/reviewer-comments?repository=example-org/homelab&number=7&page=0",
		"/github/reviewer-comments?repository=example-org/homelab&number=7&page=501",
		"/github/reviewer-comments?repository=example-org/homelab&number=x&page=1",
	} {
		if rec := doProxy(p, http.MethodGet, target, ""); rec.Code != http.StatusBadRequest {
			t.Fatalf("%s = %d", target, rec.Code)
		}
	}
	if rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/other&number=7&page=1", ""); rec.Code != http.StatusNotFound ||
		!strings.Contains(rec.Body.String(), "repository_not_bound") {
		t.Fatalf("unbound list = %d %s", rec.Code, rec.Body)
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
// edit near the size limit, and a full page of the largest comments, still
// go through.
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
	f.mu.Lock()
	for i := range CommentPageSize {
		f.comments[int64(300+i)] = ghCommentFixture{number: 8, pr: true, login: "someone", body: strings.Repeat("&", MaxCommentBody)}
	}
	f.next = 300 + CommentPageSize
	f.mu.Unlock()
	rec := doProxy(p, http.MethodGet, "/github/reviewer-comments?repository=example-org/homelab&number=8&page=1", "")
	if rec.Code != http.StatusOK || rec.Body.Len() > MaxCommentPageBytes {
		t.Fatalf("full page = %d, %d bytes", rec.Code, rec.Body.Len())
	}
	var out struct {
		Comments []ProxiedComment `json:"comments"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || len(out.Comments) != CommentPageSize ||
		len(out.Comments[0].Body) != MaxCommentBody {
		t.Fatalf("full page decoded %d comments, %v", len(out.Comments), err)
	}
}
