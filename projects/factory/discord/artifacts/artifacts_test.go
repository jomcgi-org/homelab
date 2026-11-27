package artifacts

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
)

const (
	guild   = "1501965852042330302"
	channel = "1501965852042330999"
)

// fakeS3 keeps objects in memory and checks each request is signed.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string]stored
}

type stored struct {
	body        []byte
	contentType string
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=GKtest/") ||
		r.Header.Get("X-Amz-Content-Sha256") == "" {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	key, isObject := strings.CutPrefix(r.URL.Path, "/bosun-artifacts/")
	switch {
	case r.Method == http.MethodPut && isObject:
		body, _ := io.ReadAll(r.Body)
		f.objects[key] = stored{body, r.Header.Get("Content-Type")}
	case r.Method == http.MethodDelete && isObject:
		delete(f.objects, key)
		w.WriteHeader(http.StatusNoContent)
	case r.Method == http.MethodGet && r.URL.Path == "/bosun-artifacts" && r.URL.Query().Get("list-type") == "2":
		prefix := r.URL.Query().Get("prefix")
		var keys []string
		for k := range f.objects {
			if strings.HasPrefix(k, prefix) {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		fmt.Fprint(w, `<ListBucketResult><IsTruncated>false</IsTruncated>`)
		for _, k := range keys {
			fmt.Fprintf(w, `<Contents><Key>%s</Key><Size>%d</Size></Contents>`, k, len(f.objects[k].body))
		}
		fmt.Fprint(w, `</ListBucketResult>`)
	default:
		w.WriteHeader(http.StatusBadRequest)
	}
}

type fakeVerifier struct{}

func (fakeVerifier) Verify(_ context.Context, raw string) (actorjwt.Identity, error) {
	if raw != "good" {
		return actorjwt.Identity{}, &actorjwt.Error{Reason: actorjwt.ReasonSignature}
	}
	return actorjwt.Identity{Subject: "actor/discord-" + guild + "/session-x"}, nil
}

func setup(t *testing.T) (*httptest.Server, *fakeS3) {
	t.Helper()
	fake := &fakeS3{objects: map[string]stored{}}
	s3srv := httptest.NewServer(fake)
	t.Cleanup(s3srv.Close)
	client, err := NewS3(s3srv.URL, "garage", "bosun-artifacts", "GKtest", "secret")
	if err != nil {
		t.Fatal(err)
	}
	h := &Handler{
		Store:    &Store{S3: client, BaseURL: "https://artifact.jomcgi.dev", Servers: map[string]bool{guild: true}},
		Verifier: fakeVerifier{},
		Log:      slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return srv, fake
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

func call(t *testing.T, url, tool string, args map[string]any) map[string]any {
	t.Helper()
	status, out := rpc(t, url, "good", "tools/call", map[string]any{"name": tool, "arguments": args})
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	return out["result"].(map[string]any)
}

func TestRejectsCallersWithoutAValidActorJWT(t *testing.T) {
	srv, fake := setup(t)
	for _, token := range []string{"", "bad"} {
		status, _ := rpc(t, srv.URL, token, "tools/call", map[string]any{"name": "put", "arguments": map[string]any{
			"server": guild, "channel": channel, "path": "a.html", "content": "x",
		}})
		if status != http.StatusUnauthorized {
			t.Errorf("token %q: status %d, want 401", token, status)
		}
	}
	if len(fake.objects) != 0 {
		t.Fatalf("unauthenticated put stored %v", fake.objects)
	}
}

func TestRejectsBrowserOrigins(t *testing.T) {
	srv, _ := setup(t)
	req, _ := http.NewRequest(http.MethodPost, srv.URL, strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
	req.Header.Set("Authorization", "Bearer good")
	req.Header.Set("Origin", "https://artifact.jomcgi.dev")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status %d, want 403", resp.StatusCode)
	}
}

func TestInitializeAndToolsList(t *testing.T) {
	srv, _ := setup(t)
	_, out := rpc(t, srv.URL, "good", "initialize", map[string]any{"protocolVersion": "2025-06-18"})
	if v := out["result"].(map[string]any)["protocolVersion"]; v != "2025-06-18" {
		t.Fatalf("protocolVersion %v", v)
	}
	_, out = rpc(t, srv.URL, "good", "tools/list", nil)
	var names []string
	for _, tool := range out["result"].(map[string]any)["tools"].([]any) {
		names = append(names, tool.(map[string]any)["name"].(string))
	}
	if strings.Join(names, ",") != "put,list,delete" {
		t.Fatalf("tools %v", names)
	}
}

func TestPutListDelete(t *testing.T) {
	srv, fake := setup(t)
	res := call(t, srv.URL, "put", map[string]any{
		"server": guild, "channel": channel, "path": "reports/q3.html", "content": "<h1>Q3</h1>",
	})
	if res["isError"] != false {
		t.Fatalf("put failed: %v", res)
	}
	got := res["structuredContent"].(map[string]any)
	want := "https://artifact.jomcgi.dev/" + guild + "/" + channel + "/reports/q3.html"
	if got["url"] != want {
		t.Fatalf("url %v, want %v", got["url"], want)
	}
	obj := fake.objects[guild+"/"+channel+"/reports/q3.html"]
	if obj.contentType != "text/html; charset=utf-8" || string(obj.body) != "<h1>Q3</h1>" {
		t.Fatalf("stored %q as %q", obj.body, obj.contentType)
	}

	png := base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n\x1a\nrest"))
	res = call(t, srv.URL, "put", map[string]any{
		"server": guild, "channel": channel, "path": "chart.png", "content": png, "encoding": "base64",
	})
	if res["isError"] != false || fake.objects[guild+"/"+channel+"/chart.png"].contentType != "image/png" {
		t.Fatalf("png put: %v", res)
	}

	res = call(t, srv.URL, "list", map[string]any{"server": guild, "channel": channel})
	listed := res["structuredContent"].(map[string]any)["artifacts"].([]any)
	if len(listed) != 2 || listed[0].(map[string]any)["path"] != "chart.png" {
		t.Fatalf("list %v", listed)
	}

	res = call(t, srv.URL, "delete", map[string]any{"server": guild, "channel": channel, "path": "chart.png"})
	if res["isError"] != false {
		t.Fatalf("delete: %v", res)
	}
	if _, ok := fake.objects[guild+"/"+channel+"/chart.png"]; ok {
		t.Fatal("chart.png still stored")
	}
}

func TestPutRefusals(t *testing.T) {
	srv, fake := setup(t)
	base := func(over map[string]any) map[string]any {
		args := map[string]any{"server": guild, "channel": channel, "path": "a.html", "content": "x"}
		for k, v := range over {
			args[k] = v
		}
		return args
	}
	cases := map[string]map[string]any{
		"other server":       {"server": "1111111111111111111"},
		"bad channel":        {"channel": "general"},
		"traversal":          {"path": "../a.html"},
		"hidden segment":     {"path": ".well-known/a.html"},
		"absolute":           {"path": "/a.html"},
		"empty segment":      {"path": "a//b.html"},
		"extension":          {"path": "a.js"},
		"no extension":       {"path": "index"},
		"type from argument": {"contentType": "text/html"},
		"not png":            {"path": "a.png", "content": base64.StdEncoding.EncodeToString([]byte("<svg/>")), "encoding": "base64"},
		"bad base64":         {"content": "***", "encoding": "base64"},
		"not utf-8":          {"content": base64.StdEncoding.EncodeToString([]byte{0xff, 0xfe}), "encoding": "base64"},
		"empty":              {"content": ""},
		"too large":          {"content": strings.Repeat("a", MaxBytes+1)},
	}
	for name, over := range cases {
		res := call(t, srv.URL, "put", base(over))
		if res["isError"] != true {
			t.Errorf("%s: accepted: %v", name, res)
		}
	}
	if len(fake.objects) != 0 {
		t.Fatalf("refused puts stored %v", fake.objects)
	}
}

func TestContentTypeComesFromTheExtension(t *testing.T) {
	for p, want := range map[string]string{
		"a.md": "text/plain; charset=utf-8", "A.HTML": "text/html; charset=utf-8",
		"d.csv": "text/csv; charset=utf-8", "e.svg": "image/svg+xml", "f.json": "application/json",
	} {
		got, err := CleanPath(p)
		if err != nil || got != want {
			t.Errorf("%s: %q %v, want %q", p, got, err, want)
		}
	}
}
