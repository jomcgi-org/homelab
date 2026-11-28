package notes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

const (
	guild   = "1501965852042330302"
	other   = "1512814732392927463"
	channel = "1501965852042330999"
)

// fakeS3 keeps objects in memory and checks each request is signed. down
// makes every request fail.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string]string
	down    bool
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=GKtest/") {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	key, isObject := strings.CutPrefix(r.URL.Path, "/bosun-notes/")
	switch {
	case r.Method == http.MethodPut && isObject:
		body, _ := io.ReadAll(r.Body)
		f.objects[key] = string(body)
	case r.Method == http.MethodGet && isObject:
		body, ok := f.objects[key]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `<Error><Code>NoSuchKey</Code></Error>`)
			return
		}
		fmt.Fprint(w, body)
	case r.Method == http.MethodDelete && isObject:
		delete(f.objects, key)
		w.WriteHeader(http.StatusNoContent)
	case r.Method == http.MethodGet && r.URL.Path == "/bosun-notes" && r.URL.Query().Get("list-type") == "2":
		// Pages of max-keys; the continuation token is the last key sent.
		prefix, after := r.URL.Query().Get("prefix"), r.URL.Query().Get("continuation-token")
		limit, _ := strconv.Atoi(r.URL.Query().Get("max-keys"))
		var keys []string
		for k := range f.objects {
			if strings.HasPrefix(k, prefix) && k > after {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		truncated := len(keys) > limit
		if truncated {
			keys = keys[:limit]
		}
		fmt.Fprintf(w, `<ListBucketResult><IsTruncated>%v</IsTruncated>`, truncated)
		if truncated {
			fmt.Fprintf(w, `<NextContinuationToken>%s</NextContinuationToken>`, keys[len(keys)-1])
		}
		for _, k := range keys {
			fmt.Fprintf(w, `<Contents><Key>%s</Key><Size>%d</Size></Contents>`, k, len(f.objects[k]))
		}
		fmt.Fprint(w, `</ListBucketResult>`)
	default:
		w.WriteHeader(http.StatusBadRequest)
	}
}

func (f *fakeS3) keys() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for k := range f.objects {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

type fakeVerifier struct{}

func (fakeVerifier) Verify(_ context.Context, raw string) (actorjwt.Identity, error) {
	session, ok := strings.CutPrefix(raw, "good-")
	if !ok {
		return actorjwt.Identity{}, &actorjwt.Error{Reason: actorjwt.ReasonSignature}
	}
	return actorjwt.Identity{Subject: "actor/discord-" + guild + "/session-" + session, ActorName: "session-" + session}, nil
}

func newStore(t *testing.T) (*Store, *fakeS3) {
	t.Helper()
	fake := &fakeS3{objects: map[string]string{}}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	client, err := artifacts.NewS3(srv.URL, "garage", "bosun-notes", "GKtest", "secret")
	if err != nil {
		t.Fatal(err)
	}
	clock := time.Date(2026, 11, 28, 9, 0, 0, 0, time.UTC)
	return &Store{
		Bucket:  client,
		Servers: map[string]bool{guild: true, other: true},
		Now:     func() time.Time { clock = clock.Add(time.Second); return clock },
	}, fake
}

// setup serves the tools behind a fake bridge that knows session "s1" in
// guild and "s2" in other.
func setup(t *testing.T) (string, *fakeS3) {
	t.Helper()
	store, fake := newStore(t)
	bridge := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/sessions/s1":
			fmt.Fprintf(w, `{"server_id":%q,"channel_id":%q}`, guild, channel)
		case "/sessions/s2":
			fmt.Fprintf(w, `{"server_id":%q,"channel_id":%q}`, other, channel)
		case "/sessions/s3":
			fmt.Fprintf(w, `{"server_id":"999999999999999999","channel_id":%q}`, channel)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(bridge.Close)
	h := &Handler{
		Store:    store,
		Places:   &BridgePlaces{URL: bridge.URL},
		Verifier: fakeVerifier{},
		Log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return srv.URL, fake
}

func rpc(t *testing.T, url, token, method string, params any) (int, map[string]any) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
	req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader(string(body)))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

// call runs a tool as session and returns its structured result, or its
// error text with ok false.
func call(t *testing.T, url, session, tool string, args map[string]any) (map[string]any, string, bool) {
	t.Helper()
	status, out := rpc(t, url, "good-"+session, "tools/call", map[string]any{"name": tool, "arguments": args})
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	res := out["result"].(map[string]any)
	if res["isError"] == true {
		return nil, res["content"].([]any)[0].(map[string]any)["text"].(string), false
	}
	return res["structuredContent"].(map[string]any), "", true
}

func mustCall(t *testing.T, url, session, tool string, args map[string]any) map[string]any {
	t.Helper()
	out, msg, ok := call(t, url, session, tool, args)
	if !ok {
		t.Fatalf("%s: %s", tool, msg)
	}
	return out
}

func TestRejectsCallersWithoutAValidActorJWT(t *testing.T) {
	url, _ := setup(t)
	for _, token := range []string{"", "bad"} {
		if status, _ := rpc(t, url, token, "tools/list", nil); status != http.StatusUnauthorized {
			t.Errorf("token %q: status %d, want 401", token, status)
		}
	}
}

func TestToolsListNamesNoServerArgument(t *testing.T) {
	url, _ := setup(t)
	_, out := rpc(t, url, "good-s1", "tools/list", nil)
	tools := out["result"].(map[string]any)["tools"].([]any)
	var names []string
	for _, raw := range tools {
		tool := raw.(map[string]any)
		names = append(names, tool["name"].(string))
		props := tool["inputSchema"].(map[string]any)["properties"].(map[string]any)
		for _, p := range []string{"server", "server_id", "guild"} {
			if _, ok := props[p]; ok {
				t.Errorf("%s takes %s", tool["name"], p)
			}
		}
	}
	want := "notes_list notes_read notes_search notes_write notes_append notes_delete memory_read memory_write"
	if strings.Join(names, " ") != want {
		t.Errorf("tools = %v", names)
	}
}

// The server comes from the session the bridge recorded, and an argument
// naming another server is refused.
func TestEachSessionWorksInItsOwnServersVault(t *testing.T) {
	url, fake := setup(t)
	mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Groceries", "content": "- milk"})
	mustCall(t, url, "s2", "notes_write", map[string]any{"path": "Groceries", "content": "- tea"})
	if got := fake.keys(); strings.Join(got, " ") != guild+"/Groceries.md "+other+"/Groceries.md" && strings.Join(got, " ") != other+"/Groceries.md "+guild+"/Groceries.md" {
		t.Fatalf("keys = %v", got)
	}
	if r := mustCall(t, url, "s1", "notes_read", map[string]any{"path": "Groceries.md"}); r["content"] != "- milk" {
		t.Errorf("s1 read %v", r)
	}
	if _, msg, ok := call(t, url, "s1", "notes_read", map[string]any{"path": "Groceries", "server": other}); ok || !strings.Contains(msg, "unknown field") {
		t.Errorf("server argument: ok=%v %q", ok, msg)
	}
	if _, msg, ok := call(t, url, "s1", "notes_read", map[string]any{"path": "../" + other + "/Groceries"}); ok || !strings.Contains(msg, "path") {
		t.Errorf("path escape: ok=%v %q", ok, msg)
	}
	// A session the bridge does not know, or a server off the allowlist,
	// gets no vault.
	for _, s := range []string{"unknown", "s3"} {
		if _, _, ok := call(t, url, s, "notes_list", map[string]any{}); ok {
			t.Errorf("session %s listed a vault", s)
		}
	}
}

func TestWriteAppendListSearch(t *testing.T) {
	url, fake := setup(t)
	mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Trips/Skye 2027", "content": "Ferry from [[Mallaig]].\nBook early."})
	if _, msg, ok := call(t, url, "s1", "notes_write", map[string]any{"path": "Trips/Skye 2027.md", "content": "x"}); ok || !strings.Contains(msg, "exists") {
		t.Errorf("overwrite without replace: ok=%v %q", ok, msg)
	}
	r := mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Trips/Skye 2027.md", "content": "Ferry from [[Mallaig]].", "replace": true})
	if r["replaced"] != true {
		t.Errorf("replace = %v", r)
	}
	mustCall(t, url, "s1", "notes_append", map[string]any{"path": "Trips/Skye 2027", "content": "Midges in June."})
	mustCall(t, url, "s1", "notes_append", map[string]any{"path": "Mallaig", "content": "Port town."})
	got := mustCall(t, url, "s1", "notes_read", map[string]any{"path": "Trips/Skye 2027"})["content"]
	if got != "Ferry from [[Mallaig]].\nMidges in June." {
		t.Errorf("after append: %q", got)
	}
	// The replaced version is kept.
	var history []string
	for _, k := range fake.keys() {
		if strings.HasPrefix(k, guild+"/.history/Trips/Skye 2027/") {
			history = append(history, fake.objects[k])
		}
	}
	if len(history) != 1 || history[0] != "Ferry from [[Mallaig]].\nBook early." {
		t.Errorf("history = %q", history)
	}
	list := mustCall(t, url, "s1", "notes_list", map[string]any{})["notes"].([]any)
	if len(list) != 2 {
		t.Errorf("list = %v, want two notes without .history", list)
	}
	trips := mustCall(t, url, "s1", "notes_list", map[string]any{"folder": "Trips"})["notes"].([]any)
	if len(trips) != 1 || trips[0].(map[string]any)["path"] != "Trips/Skye 2027.md" {
		t.Errorf("folder list = %v", trips)
	}
	hits := mustCall(t, url, "s1", "notes_search", map[string]any{"query": "MIDGES"})["hits"].([]any)
	if len(hits) != 1 || !strings.Contains(hits[0].(map[string]any)["snippet"].(string), "Midges in June") {
		t.Errorf("search = %v", hits)
	}
	if hits := mustCall(t, url, "s1", "notes_search", map[string]any{"query": "mallaig"})["hits"].([]any); len(hits) != 2 {
		t.Errorf("search by name and text = %v", hits)
	}
	if hits := mustCall(t, url, "s2", "notes_search", map[string]any{"query": "midges"})["hits"].([]any); len(hits) != 0 {
		t.Errorf("other server found %v", hits)
	}
}

func TestReservedFoldersAreReadOnly(t *testing.T) {
	url, _ := setup(t)
	for _, p := range []string{"_hermes/MEMORY.md", ".trash/x", ".history/x/y", "_hermes/new"} {
		for _, tool := range []string{"notes_write", "notes_append"} {
			if _, _, ok := call(t, url, "s1", tool, map[string]any{"path": p, "content": "x"}); ok {
				t.Errorf("%s %s succeeded", tool, p)
			}
		}
		if _, _, ok := call(t, url, "s1", "notes_delete", map[string]any{"path": p}); ok {
			t.Errorf("delete %s succeeded", p)
		}
	}
}

// Delete moves a note to .trash/, adds a time to a taken name, and hides it
// from list and search unless asked.
func TestDeleteMovesToTrash(t *testing.T) {
	url, fake := setup(t)
	mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Ideas", "content": "first idea"})
	r := mustCall(t, url, "s1", "notes_delete", map[string]any{"path": "Ideas"})
	if r["trashedAs"] != ".trash/Ideas.md" {
		t.Errorf("first delete = %v", r)
	}
	mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Ideas", "content": "second idea"})
	r = mustCall(t, url, "s1", "notes_delete", map[string]any{"path": "Ideas"})
	second, _ := r["trashedAs"].(string)
	if !strings.HasPrefix(second, ".trash/Ideas.2026") || !strings.HasSuffix(second, "Z.md") {
		t.Errorf("second delete = %v", r)
	}
	fake.mu.Lock()
	first, kept := fake.objects[guild+"/.trash/Ideas.md"], fake.objects[guild+"/"+second]
	_, live := fake.objects[guild+"/Ideas.md"]
	fake.mu.Unlock()
	if first != "first idea" || kept != "second idea" || live {
		t.Errorf("trash = %q %q, live %v", first, kept, live)
	}
	if n := mustCall(t, url, "s1", "notes_list", map[string]any{})["notes"].([]any); len(n) != 0 {
		t.Errorf("list shows trash: %v", n)
	}
	if n := mustCall(t, url, "s1", "notes_list", map[string]any{"include_trash": true})["notes"].([]any); len(n) != 2 {
		t.Errorf("list with trash = %v", n)
	}
	if h := mustCall(t, url, "s1", "notes_search", map[string]any{"query": "idea"})["hits"].([]any); len(h) != 0 {
		t.Errorf("search shows trash: %v", h)
	}
	if h := mustCall(t, url, "s1", "notes_search", map[string]any{"query": "idea", "include_trash": true})["hits"].([]any); len(h) != 2 {
		t.Errorf("search with trash = %v", h)
	}
	if r := mustCall(t, url, "s1", "notes_read", map[string]any{"path": ".trash/Ideas.md"}); r["content"] != "first idea" {
		t.Errorf("read trash = %v", r)
	}
	if _, _, ok := call(t, url, "s1", "notes_delete", map[string]any{"path": "Ideas"}); ok {
		t.Error("deleting a missing note succeeded")
	}
}

// memory_write replaces a file only from its current version; a stale base
// changes nothing and returns the newer copy.
func TestMemoryWriteChecksTheVersion(t *testing.T) {
	url, fake := setup(t)
	r := mustCall(t, url, "s1", "memory_read", map[string]any{"file": "MEMORY.md"})
	if r["content"] != "" || r["version"] != "" {
		t.Fatalf("missing file = %v", r)
	}
	w := mustCall(t, url, "s1", "memory_write", map[string]any{"file": "MEMORY.md", "content": "a", "base_version": ""})
	v1 := w["version"].(string)
	if v1 != Version("a", true) {
		t.Fatalf("version = %v", w)
	}
	stale := mustCall(t, url, "s1", "memory_write", map[string]any{"file": "MEMORY.md", "content": "b", "base_version": ""})
	if stale["conflict"] != true || stale["content"] != "a" || stale["version"] != v1 {
		t.Fatalf("stale write = %v", stale)
	}
	mustCall(t, url, "s1", "memory_write", map[string]any{"file": "MEMORY.md", "content": "a\nb", "base_version": v1})
	fake.mu.Lock()
	current := fake.objects[guild+"/_hermes/MEMORY.md"]
	var kept int
	for k := range fake.objects {
		if strings.HasPrefix(k, guild+"/.history/_hermes/MEMORY/") {
			kept++
		}
	}
	fake.mu.Unlock()
	if current != "a\nb" || kept != 1 {
		t.Errorf("current %q, kept %d", current, kept)
	}
	if _, _, ok := call(t, url, "s1", "memory_read", map[string]any{"file": "SOUL.md"}); ok {
		t.Error("read SOUL.md")
	}
	// Members read the files as notes.
	if r := mustCall(t, url, "s1", "notes_read", map[string]any{"path": "_hermes/MEMORY.md"}); r["content"] != "a\nb" {
		t.Errorf("notes_read memory = %v", r)
	}
}

// Concurrent appends to one note lose nothing: one replica, one lock a key.
func TestConcurrentAppendsKeepEveryLine(t *testing.T) {
	store, _ := newStore(t)
	ctx := context.Background()
	var wg sync.WaitGroup
	for i := range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := store.Append(ctx, guild, "Log", fmt.Sprintf("line %d", i)); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	r, err := store.Read(ctx, guild, "Log")
	if err != nil {
		t.Fatal(err)
	}
	if n := len(strings.Split(r.Content, "\n")); n != 20 {
		t.Errorf("%d lines, want 20", n)
	}
}

func TestStorageErrorsReachTheCallerAsAToolError(t *testing.T) {
	url, fake := setup(t)
	fake.mu.Lock()
	fake.down = true
	fake.mu.Unlock()
	if _, msg, ok := call(t, url, "s1", "notes_list", map[string]any{}); ok || msg != "Storage error; try again." {
		t.Errorf("outage: ok=%v %q", ok, msg)
	}
}

func TestCleanPath(t *testing.T) {
	for in, want := range map[string]string{
		"Groceries":           "Groceries.md",
		"Trips/Skye 2027.MD":  "Trips/Skye 2027.md",
		"/Ideas.md":           "Ideas.md",
		"v1.2 notes":          "v1.2 notes.md",
		"Café (old)":          "Café (old).md",
		"_hermes/USER.md":     "_hermes/USER.md",
		".trash/Ideas.md|res": "",
	} {
		reserved := strings.HasSuffix(in, "|res")
		got, err := CleanPath(strings.TrimSuffix(in, "|res"), reserved)
		if reserved {
			want = ".trash/Ideas.md"
		}
		if err != nil || got != want {
			t.Errorf("CleanPath(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"", "../x", "a/../b", ".trash/x", "a//b", "a/ lead", "a/b/c/d/e/f/g/h/i", strings.Repeat("a", 201), "x\x00y", ".hidden"} {
		var pe *ParamError
		if _, err := CleanPath(in, false); !errors.As(err, &pe) {
			t.Errorf("CleanPath(%q) err = %v, want a ParamError", in, err)
		}
	}
}

// Many trash and history objects sort before the notes; a listing pages
// past them.
func TestListPagesPastHiddenFolders(t *testing.T) {
	url, fake := setup(t)
	fake.mu.Lock()
	for i := range 2500 {
		fake.objects[fmt.Sprintf("%s/.history/Log/%05d.md", guild, i)] = "old"
	}
	fake.mu.Unlock()
	mustCall(t, url, "s1", "notes_write", map[string]any{"path": "Groceries", "content": "milk"})
	list := mustCall(t, url, "s1", "notes_list", map[string]any{})
	if n := list["notes"].([]any); len(n) != 1 || list["truncated"] == true {
		t.Fatalf("list = %v", list)
	}
	if h := mustCall(t, url, "s1", "notes_search", map[string]any{"query": "milk"})["hits"].([]any); len(h) != 1 {
		t.Errorf("search = %v", h)
	}
}

// A bridge that fails is a storage error, not a missing vault.
func TestABridgeFailureIsAStorageError(t *testing.T) {
	store, _ := newStore(t)
	bridge := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	t.Cleanup(bridge.Close)
	h := &Handler{Store: store, Places: &BridgePlaces{URL: bridge.URL}, Verifier: fakeVerifier{}, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	if _, msg, ok := call(t, srv.URL, "s1", "notes_list", map[string]any{}); ok || msg != "Storage error; try again." {
		t.Errorf("bridge down: ok=%v %q", ok, msg)
	}
}
