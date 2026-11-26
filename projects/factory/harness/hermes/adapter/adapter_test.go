package adapter

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

const testKey = "0123456789abcdef0123456789abcdef"

type hermesCall struct {
	Path        string
	Auth        string
	SessionKey  string
	Idempotency string
	Body        responsesRequest
}

// fakeHermes serves /v1/responses with a fixed reply and records each call.
type fakeHermes struct {
	mu     sync.Mutex
	calls  []hermesCall
	status int
	reply  string
}

func (f *fakeHermes) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	data, _ := io.ReadAll(r.Body)
	var body responsesRequest
	_ = json.Unmarshal(data, &body)
	f.mu.Lock()
	f.calls = append(f.calls, hermesCall{
		Path: r.URL.Path, Auth: r.Header.Get("Authorization"), SessionKey: r.Header.Get(SessionKeyHeader),
		Idempotency: r.Header.Get("Idempotency-Key"), Body: body,
	})
	f.mu.Unlock()
	status := f.status
	if status == 0 {
		status = http.StatusOK
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, f.reply)
}

func (f *fakeHermes) Calls() []hermesCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.calls)
}

const completed = `{"id":"resp_1","object":"response","status":"completed","output":[
 {"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"thinking... "}]},
 {"type":"message","role":"assistant","content":[{"type":"output_text","text":"Hello from Hermes"}]}]}`

// newHarness starts a fake Hermes and the adapter's A2A handler in front of it.
func newHarness(t *testing.T, hermes *fakeHermes) (*httptest.Server, *kagent.A2A) {
	t.Helper()
	hs := httptest.NewServer(hermes)
	t.Cleanup(hs.Close)
	a2a := httptest.NewServer(&Handler{Hermes: &Client{BaseURL: hs.URL, Key: testKey}})
	t.Cleanup(a2a.Close)
	return a2a, &kagent.A2A{BaseURL: a2a.URL}
}

var agent = kagent.AgentRef{Namespace: "discord-homelab", Name: "hermes"}

func TestSendMessageMakesOneResponsesCallWithTheSessionHeader(t *testing.T) {
	hermes := &fakeHermes{reply: completed}
	_, client := newHarness(t, hermes)

	task, err := client.Send(context.Background(), agent, kagent.Message{
		MessageID: "m-1", ContextID: "ctx-guild-42", Parts: []kagent.Part{{Text: "What runs the factory?"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if task.Status.State != kagent.StateCompleted || task.ContextID != "ctx-guild-42" {
		t.Fatalf("task = %+v", task)
	}
	if got := task.Status.Message.Parts[0].Text; got != "Hello from Hermes" {
		t.Fatalf("answer = %q, want the final message without commentary", got)
	}
	if task.Status.Message.Role != RoleAgent || len(task.Artifacts) != 1 || task.Artifacts[0].Parts[0].Text != "Hello from Hermes" {
		t.Fatalf("task = %+v", task)
	}
	calls := hermes.Calls()
	if len(calls) != 1 {
		t.Fatalf("calls = %d, want 1", len(calls))
	}
	c := calls[0]
	if c.Path != "/v1/responses" || c.Auth != "Bearer "+testKey || c.SessionKey != "ctx-guild-42" {
		t.Fatalf("call = %+v", c)
	}
	want := responsesRequest{Input: "What runs the factory?", Store: true, Conversation: "ctx-guild-42"}
	if c.Body != want {
		t.Fatalf("body = %+v, want %+v", c.Body, want)
	}
	if !strings.HasPrefix(c.Idempotency, "a2a-") || len(c.Idempotency) > 255 {
		t.Fatalf("Idempotency-Key = %q", c.Idempotency)
	}
}

func TestRetriedMessageReusesItsIdempotencyKey(t *testing.T) {
	hermes := &fakeHermes{reply: completed}
	_, client := newHarness(t, hermes)
	msg := kagent.Message{MessageID: "m-1", ContextID: "ctx", Parts: []kagent.Part{{Text: "hi"}}}
	for range 2 {
		if _, err := client.Send(context.Background(), agent, msg); err != nil {
			t.Fatal(err)
		}
	}
	msg.MessageID = "m-2"
	if _, err := client.Send(context.Background(), agent, msg); err != nil {
		t.Fatal(err)
	}
	calls := hermes.Calls()
	if calls[0].Idempotency != calls[1].Idempotency || calls[1].Idempotency == calls[2].Idempotency {
		t.Fatalf("keys = %q %q %q", calls[0].Idempotency, calls[1].Idempotency, calls[2].Idempotency)
	}
}

func TestMessageWithoutContextGetsOne(t *testing.T) {
	hermes := &fakeHermes{reply: completed}
	_, client := newHarness(t, hermes)
	task, err := client.Send(context.Background(), agent, kagent.Message{MessageID: "m-1", Parts: []kagent.Part{{Text: "hi"}}})
	if err != nil {
		t.Fatal(err)
	}
	if task.ContextID == "" || hermes.Calls()[0].SessionKey != task.ContextID {
		t.Fatalf("context = %q, session key = %q", task.ContextID, hermes.Calls()[0].SessionKey)
	}
}

func TestTurnThatDoesNotSettleFailsWithoutWaiting(t *testing.T) {
	for _, reply := range []string{
		`{"status":"in_progress","output":[]}`,
		`{"status":"waiting_for_approval","output":[]}`,
		`{"status":"requires_action","output":[]}`,
	} {
		hermes := &fakeHermes{reply: reply}
		_, client := newHarness(t, hermes)
		task, err := client.Send(context.Background(), agent, kagent.Message{MessageID: "m", ContextID: "c", Parts: []kagent.Part{{Text: "run rm -rf /"}}})
		if err != nil {
			t.Fatal(err)
		}
		if task.Status.State != kagent.StateFailed {
			t.Fatalf("%s: state = %s, want failed", reply, task.Status.State)
		}
		if n := len(hermes.Calls()); n != 1 {
			t.Fatalf("%s: calls = %d, want 1 (no polling)", reply, n)
		}
	}
}

func TestHermesFailuresFailTheTask(t *testing.T) {
	for name, hermes := range map[string]*fakeHermes{
		"failed":    {reply: `{"status":"failed","error":{"message":"provider 502"},"output":[]}`},
		"cancelled": {reply: `{"status":"cancelled","output":[]}`},
		"http 429":  {status: http.StatusTooManyRequests, reply: `{"error":{"message":"Too many concurrent runs (max 10)"}}`},
		"no answer": {reply: `{"status":"completed","output":[]}`},
		"not json":  {reply: `<html>`},
	} {
		_, client := newHarness(t, hermes)
		task, err := client.Send(context.Background(), agent, kagent.Message{MessageID: "m", ContextID: "c", Parts: []kagent.Part{{Text: "hi"}}})
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if task.Status.State != kagent.StateFailed || task.Status.Message.Parts[0].Text != "Hermes turn failed" {
			t.Fatalf("%s: task = %+v", name, task.Status)
		}
	}
}

func TestIncompleteTurnKeepsItsWrapUp(t *testing.T) {
	hermes := &fakeHermes{reply: `{"status":"incomplete","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Partial answer"}]}]}`}
	_, client := newHarness(t, hermes)
	task, err := client.Send(context.Background(), agent, kagent.Message{MessageID: "m", ContextID: "c", Parts: []kagent.Part{{Text: "hi"}}})
	if err != nil {
		t.Fatal(err)
	}
	if task.Status.State != kagent.StateCompleted || task.Status.Message.Parts[0].Text != "Partial answer" {
		t.Fatalf("task = %+v", task.Status)
	}
}

func TestInvalidRequestsAreRefused(t *testing.T) {
	hermes := &fakeHermes{reply: completed}
	srv, _ := newHarness(t, hermes)
	for name, tc := range map[string]struct {
		body, version string
		code          int
	}{
		"other method":  {`{"jsonrpc":"2.0","id":"1","method":"GetTask","params":{}}`, "1.0", codeMethodNotFound},
		"agent role":    {`{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m","role":"ROLE_AGENT","parts":[{"text":"x"}]}}}`, "1.0", codeInvalidParams},
		"task id":       {`{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m","taskId":"t","role":"ROLE_USER","parts":[{"text":"x"}]}}}`, "1.0", codeInvalidParams},
		"two parts":     {`{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m","role":"ROLE_USER","parts":[{"text":"x"},{"text":"y"}]}}}`, "", codeInvalidParams},
		"no message id": {`{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"role":"ROLE_USER","parts":[{"text":"x"}]}}}`, "1.0", codeInvalidParams},
		"bad context":   {`{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m","contextId":"a\nb","role":"ROLE_USER","parts":[{"text":"x"}]}}}`, "1.0", codeInvalidParams},
		"not json":      {`{`, "1.0", codeParse},
	} {
		req, _ := http.NewRequest(http.MethodPost, srv.URL, strings.NewReader(tc.body))
		if tc.version != "" {
			req.Header.Set("A2A-Version", tc.version)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		var out rpcResponse
		var raw struct {
			Error *rpcError `json:"error"`
		}
		data, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		_ = json.Unmarshal(data, &out)
		_ = json.Unmarshal(data, &raw)
		if raw.Error == nil || raw.Error.Code != tc.code {
			t.Fatalf("%s: response = %s, want code %d", name, data, tc.code)
		}
	}
	if n := len(hermes.Calls()); n != 0 {
		t.Fatalf("calls = %d, want 0", n)
	}
	req, _ := http.NewRequest(http.MethodPost, srv.URL, strings.NewReader(`{}`))
	req.Header.Set("A2A-Version", "0.3")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("A2A-Version 0.3: HTTP %d", resp.StatusCode)
	}
}

func TestRenderedConfigTurnsToolsAndMessagingOff(t *testing.T) {
	data, err := RenderConfig(Settings{Model: "deepseek/deepseek-v4.1-flash-1017:online"})
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		t.Fatal(err)
	}
	model := cfg["model"].(map[string]any)
	if model["provider"] != "custom" || model["base_url"] != DefaultBaseURL || model["api_key"] != PlaceholderAPIKey || model["default"] != "deepseek/deepseek-v4.1-flash-1017:online" {
		t.Fatalf("model = %v", model)
	}
	// An explicit empty platform list is what turns every toolset off.
	platform, ok := cfg["platform_toolsets"].(map[string]any)["api_server"].([]any)
	if !ok || len(platform) != 0 {
		t.Fatalf("platform_toolsets.api_server = %#v, want an explicit empty list", cfg["platform_toolsets"])
	}
	if ts, ok := cfg["toolsets"].([]any); !ok || len(ts) != 0 {
		t.Fatalf("toolsets = %#v", cfg["toolsets"])
	}
	disabled := cfg["agent"].(map[string]any)["disabled_toolsets"].([]any)
	for _, name := range []string{"terminal", "file", "code_execution", "browser", "web", "delegation", "cronjob", "skills", "discord", "discord_admin", "memory"} {
		if !slices.Contains(disabled, any(name)) {
			t.Errorf("disabled_toolsets lacks %s", name)
		}
	}
	memory := cfg["memory"].(map[string]any)
	if memory["memory_enabled"] != false || memory["user_profile_enabled"] != false || memory["provider"] != "" {
		t.Fatalf("memory = %v", memory)
	}
	approvals := cfg["approvals"].(map[string]any)
	if approvals["unattended_mode"] != "deny" || approvals["cron_mode"] != "deny" || approvals["mode"] != "manual" {
		t.Fatalf("approvals = %v", approvals)
	}
	if cfg["security"].(map[string]any)["allow_lazy_installs"] != false {
		t.Fatalf("security = %v", cfg["security"])
	}
	// No messaging platform is configured: gateway platforms come only from
	// the API_SERVER_* environment the adapter sets.
	for _, key := range []string{"gateway", "platforms", "discord", "mcp_servers"} {
		if _, ok := cfg[key]; ok {
			t.Errorf("config has %q", key)
		}
	}
}

func TestWriteHomeRendersHindsightContextMode(t *testing.T) {
	home := t.TempDir()
	if err := WriteHome(home, Settings{Model: "m", BaseURL: "http://127.0.0.1:9/v1"}); err != nil {
		t.Fatal(err)
	}
	var hs map[string]string
	data, err := os.ReadFile(filepath.Join(home, "hindsight", "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &hs); err != nil || hs["memory_mode"] != "context" {
		t.Fatalf("hindsight config = %s (%v)", data, err)
	}
	info, err := os.Stat(filepath.Join(home, "config.yaml"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("config.yaml mode = %v (%v)", info.Mode(), err)
	}
}

func TestSettingsAreValidated(t *testing.T) {
	for _, s := range []Settings{{}, {Model: "m", BaseURL: "openrouter.ai"}, {Model: "m", BaseURL: "ftp://x/v1"}} {
		if _, err := RenderConfig(s); err == nil {
			t.Errorf("RenderConfig(%+v) succeeded", s)
		}
	}
}

func TestGatewayEnvironmentDropsPlatformTokens(t *testing.T) {
	g := &Gateway{Home: "/data/hermes", Port: 8642, Key: testKey}
	env := g.Environment([]string{
		"PATH=/usr/bin", "SSL_CERT_FILE=/run/kagent/egress/trust-bundle.pem", "OTEL_EXPORTER_OTLP_ENDPOINT=http://c:4317",
		"DISCORD_BOT_TOKEN=secret", "HERMES_YOLO_MODE=1", "API_SERVER_HOST=0.0.0.0", "TELEGRAM_BOT_TOKEN=x", "A2A_PORT=9900",
	})
	for _, want := range []string{
		"PATH=/usr/bin", "SSL_CERT_FILE=/run/kagent/egress/trust-bundle.pem", "OTEL_EXPORTER_OTLP_ENDPOINT=http://c:4317",
		"HERMES_HOME=/data/hermes", "API_SERVER_ENABLED=true", "API_SERVER_HOST=127.0.0.1", "API_SERVER_PORT=8642", "API_SERVER_KEY=" + testKey,
	} {
		if !slices.Contains(env, want) {
			t.Errorf("environment lacks %s", want)
		}
	}
	for _, kv := range env {
		for _, banned := range []string{"DISCORD_", "TELEGRAM_", "HERMES_YOLO", "A2A_", "API_SERVER_HOST=0.0.0.0"} {
			if strings.HasPrefix(kv, banned) {
				t.Errorf("environment keeps %s", kv)
			}
		}
	}
}

func TestNewKeyIsLongEnoughForHermes(t *testing.T) {
	k, err := NewKey()
	if err != nil || len(k) < 16 {
		t.Fatalf("key %q (%v)", k, err)
	}
}
