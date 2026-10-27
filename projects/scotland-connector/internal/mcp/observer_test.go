package mcp

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// Runs the actual rendered production filter against the Scotland handler.
// Bazel supplies pinned Linux binaries. Ordinary go test can opt in with env.
func TestNativeObserverInteroperability(t *testing.T) {
	envoy := os.Getenv("ENVOY_MCP_BIN")
	if envoy == "" {
		t.Skip("ENVOY_MCP_BIN supplied by Bazel on Linux")
	}
	helm := os.Getenv("HELM_BIN")
	chart := os.Getenv("OBSERVER_CHART")
	if src := os.Getenv("TEST_SRCDIR"); src != "" {
		workspace := os.Getenv("TEST_WORKSPACE")
		resolve := func(p string) string {
			if filepath.IsAbs(p) {
				return p
			}
			return filepath.Join(src, workspace, p)
		}
		envoy, helm, chart = resolve(envoy), resolve(helm), resolve(chart)
	}
	var count atomic.Int64
	h := handler()
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		count.Add(1)
		if r.Header.Get("X-Test-Disconnect") == "true" {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		h.ServeHTTP(w, r)
	}))
	defer backend.Close()
	port := func() string {
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer l.Close()
		return fmt.Sprint(l.Addr().(*net.TCPAddr).Port)
	}
	listenPort, adminPort := port(), port()
	rendered, err := exec.Command(helm, "template", "scotland-connector", chart, "--namespace", "scotland-connector", "--set", "config.r2Endpoint=https://example.test", "--set", "mcpObserver.enabled=true", "--show-only", "templates/mcp-observer.yaml").CombinedOutput()
	if err != nil {
		t.Fatalf("render: %v: %s", err, rendered)
	}
	_, config, ok := strings.Cut(string(rendered), "envoy.yaml: |\n")
	if !ok {
		t.Fatal("missing config")
	}
	config, _, _ = strings.Cut(config, "\n---")
	lines := strings.Split(config, "\n")
	for i, line := range lines {
		lines[i] = strings.TrimPrefix(line, "    ")
	}
	config = strings.Join(lines, "\n") + "\n"
	config = strings.ReplaceAll(config, "port_value: 8080", "port_value: "+listenPort)
	config = strings.ReplaceAll(config, "port_value: 9901", "port_value: "+adminPort)
	config = strings.ReplaceAll(config, "scotland-connector.scotland-connector.svc", "127.0.0.1")
	_, backendPort, _ := net.SplitHostPort(strings.TrimPrefix(backend.URL, "http://"))
	config = strings.ReplaceAll(config, "port_value: 80\n", "port_value: "+backendPort+"\n")
	dir := t.TempDir()
	path := filepath.Join(dir, "envoy.yaml")
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	logs, err := os.Create(filepath.Join(dir, "access.log"))
	if err != nil {
		t.Fatal(err)
	}
	defer logs.Close()
	cmd := exec.Command(envoy, "-c", path, "--concurrency", "1", "--disable-hot-restart", "--log-level", "error")
	cmd.Stdout = logs
	cmd.Stderr = logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cmd.Process.Kill(); cmd.Wait() }()
	client := &http.Client{Timeout: 3 * time.Second}
	ready := false
	for i := 0; i < 100; i++ {
		res, e := client.Get("http://127.0.0.1:" + adminPort + "/ready")
		if e == nil {
			res.Body.Close()
			if res.StatusCode == 200 {
				ready = true
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !ready {
		b, _ := os.ReadFile(logs.Name())
		t.Fatalf("envoy not ready: %s", b)
	}
	type testCase struct {
		name, body string
		headers    map[string]string
	}
	modernBody := func(method, name, args string) string {
		params := modernMeta
		if name != "" {
			params += `,"name":"` + name + `","arguments":` + args
		}
		return `{"jsonrpc":"2.0","id":1,"method":"` + method + `","params":{` + params + `}}`
	}
	cases := []testCase{
		{"initialize", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}`, nil},
		{"discover", modernBody("server/discover", "", ""), modern("server/discover", "")},
		{"list", modernBody("tools/list", "", ""), modern("tools/list", "")},
		{"plan", modernBody("tools/call", "plan", `{"lat":57.19,"lon":-3.83,"from":"2026-10-20","to":"2026-10-20"}`), modern("tools/call", "plan")},
		{"check", modernBody("tools/call", "check", `{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"Cairn Gorm"}]}`), modern("tools/call", "check")},
		{"aurora-off", modernBody("tools/call", "aurora", `{"lat":57.3,"lon":-3.7,"at":"2026-10-19T20:00:00Z"}`), modern("tools/call", "aurora")},
		{"isError", modernBody("tools/call", "plan", `{}`), modern("tools/call", "plan")},
		{"unknown-tool", modernBody("tools/call", "private_secret_tool", `{}`), modern("tools/call", "private_secret_tool")},
		{"method-spoof", modernBody("tools/list", "", ""), modern("tools/call", "private_secret_tool")},
		{"name-spoof", modernBody("tools/call", "plan", `{}`), modern("tools/call", "private_secret_tool")},
		{"missing-header", modernBody("tools/list", "", ""), nil},
		{"version-mismatch", modernBody("tools/list", "", ""), map[string]string{"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list"}},
		{"unknown-version", `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"MCP-Protocol-Version": "2099-01-01"}},
		{"malformed", `{"jsonrpc":`, nil},
		{"batch", `[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]`, nil},
		{"oversize", `{"padding":"` + strings.Repeat("x", 65536) + `","jsonrpc":"2.0","id":1,"method":"tools/list"}`, nil},
		{"notification", `{"jsonrpc":"2.0","method":"notifications/initialized"}`, nil},
		{"duplicate", `{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"tools/list"}`, nil},
		{"origin", `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"Origin": "https://evil.example"}},
	}
	for _, v := range LegacyVersions {
		cases = append(cases, testCase{"legacy-" + v, `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"MCP-Protocol-Version": v}})
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/mcp", strings.NewReader(tc.body))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Accept", "application/json, text/event-stream")
			for k, v := range tc.headers {
				req.Header.Set(k, v)
			}
			expected := httptest.NewRecorder()
			h.ServeHTTP(expected, req)
			req, err := http.NewRequest("POST", "http://127.0.0.1:"+listenPort+"/mcp", strings.NewReader(tc.body))
			if err != nil {
				t.Fatal(err)
			}
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Accept", "application/json, text/event-stream")
			for k, v := range tc.headers {
				req.Header.Set(k, v)
			}
			before := count.Load()
			res, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer res.Body.Close()
			body, _ := io.ReadAll(res.Body)
			if res.StatusCode != expected.Code || string(body) != expected.Body.String() {
				t.Fatalf("proxy changed response: status %d/%d body %s / %s", res.StatusCode, expected.Code, body, expected.Body.String())
			}
			for _, key := range []string{"Cache-Control", "Content-Type", "MCP-Protocol-Version", "X-Content-Type-Options"} {
				if res.Header.Get(key) != expected.Header().Get(key) {
					t.Errorf("changed %s", key)
				}
			}
			if count.Load() != before+1 {
				t.Fatal("request did not reach handler exactly once")
			}
		})
	}
	// The proxy has no path to admin or private mounts, even if bypassing the edge.
	for _, path := range []string{"/metrics", "/readyz", "/admin", "/mcp/private", "/v1/plan"} {
		before := count.Load()
		res, err := client.Get("http://127.0.0.1:" + listenPort + path)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != 404 || count.Load() != before {
			t.Fatalf("path leaked upstream: %s", path)
		}
	}
	// Do not replay a POST after the upstream has accepted and disconnected.
	req, _ := http.NewRequest("POST", "http://127.0.0.1:"+listenPort+"/mcp", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"ping"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("X-Test-Disconnect", "true")
	req.Header.Set("X-Envoy-Retry-On", "reset,5xx")
	req.Header.Set("X-Envoy-Max-Retries", "3")
	before := count.Load()
	res, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 503 || count.Load() != before+1 {
		t.Fatal("upstream disconnect retried or wrong status")
	}
	// BODY metadata must report the real name even when a caller spoofs Mcp-Name.
	cmd.Process.Signal(syscall.SIGTERM)
	cmd.Wait()
	raw, _ := os.ReadFile(logs.Name())
	seen := false
	for _, line := range strings.Split(string(raw), "\n") {
		var entry map[string]any
		if json.Unmarshal([]byte(line), &entry) == nil && entry["tool"] == "plan" && entry["status"] == float64(400) {
			seen = true
		}
	}
	if !seen {
		t.Fatalf("missing body-derived spoof observation: %s", raw)
	}
}
