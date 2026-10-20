package mcp

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/prometheus/client_golang/prometheus"
)

func handler() *Handler {
	return New(fixture.Service(true), meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)),
		ServerInfo{Name: "scotland-connector", Version: "test"}, []string{"https://claude.ai"})
}

type reply struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  map[string]any  `json:"result"`
	Error   *rpcError       `json:"error"`
}

func post(t *testing.T, h http.Handler, body string, hdr map[string]string) (int, http.Header, reply) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var r reply
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &r); err != nil {
			t.Fatalf("invalid JSON %q: %v", rec.Body, err)
		}
	}
	return rec.Code, rec.Header(), r
}

const modernMeta = `"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"test","version":"1"}}`

func modern(method, name string) map[string]string {
	h := map[string]string{"MCP-Protocol-Version": ModernVersion, "Mcp-Method": method}
	if name != "" {
		h["Mcp-Name"] = name
	}
	return h
}

// The Claude directory requires a title and readOnlyHint on every tool; the
// list must also be deterministic and match the connector's surface.
func TestToolListAndAnnotations(t *testing.T) {
	code, _, r := post(t, handler(), `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{`+modernMeta+`}}`, modern("tools/list", ""))
	if code != http.StatusOK || r.Error != nil {
		t.Fatalf("status %d error %+v", code, r.Error)
	}
	if r.Result["resultType"] != "complete" || r.Result["cacheScope"] != "public" {
		t.Fatalf("modern fields missing: %v", r.Result)
	}
	tools := r.Result["tools"].([]any)
	var names []string
	for _, raw := range tools {
		tool := raw.(map[string]any)
		names = append(names, tool["name"].(string))
		if tool["title"] == "" || tool["description"] == "" {
			t.Errorf("%s: missing title or description", tool["name"])
		}
		ann := tool["annotations"].(map[string]any)
		if ann["readOnlyHint"] != true || ann["destructiveHint"] != false || ann["title"] == "" {
			t.Errorf("%s: annotations %v", tool["name"], ann)
		}
		schema := tool["inputSchema"].(map[string]any)
		if schema["type"] != "object" {
			t.Errorf("%s: inputSchema %v", tool["name"], schema)
		}
	}
	if strings.Join(names, ",") != "dark_sky_windows,walk_windows" {
		t.Fatalf("tools = %v", names)
	}
}

func TestToolSchemaCapsMatchCode(t *testing.T) {
	for _, tool := range Tools {
		var s struct {
			Properties map[string]struct {
				Minimum   *float64 `json:"minimum"`
				Maximum   *float64 `json:"maximum"`
				Default   any      `json:"default"`
				MaxLength *int     `json:"maxLength"`
				Enum      []string `json:"enum"`
			} `json:"properties"`
			Required []string `json:"required"`
			AnyOf    []struct {
				Required []string `json:"required"`
			} `json:"anyOf"`
		}
		if err := json.Unmarshal(tool.InputSchema, &s); err != nil {
			t.Fatalf("%s: %v", tool.Name, err)
		}
		caps := map[string]map[string][2]float64{
			"dark_sky_windows": {
				"radius_km": {connector.MaxRadiusKm, connector.DefaultRadiusKm},
				"nights":    {connector.MaxNights, connector.DefaultNights},
				"limit":     {connector.MaxLimit, connector.DefaultLimit},
			},
			"walk_windows": {
				"radius_km": {connector.MaxWalkRadiusKm, connector.DefaultWalkRadiusKm},
				"days":      {connector.MaxWalkDays, connector.DefaultWalkDays},
				"limit":     {connector.MaxLimit, connector.DefaultLimit},
			},
		}[tool.Name]
		if caps == nil {
			t.Fatalf("%s: no caps pinned for this tool", tool.Name)
		}
		for name, want := range caps {
			p := s.Properties[name]
			if def, ok := p.Default.(float64); p.Maximum == nil || *p.Maximum != want[0] || !ok || def != want[1] {
				t.Errorf("%s.%s = max %v default %v, code %v", tool.Name, name, p.Maximum, p.Default, want)
			}
		}
		if tool.Name == "walk_windows" {
			if p := s.Properties["max_duration_h"]; p.Maximum == nil || *p.Maximum != connector.MaxWalkDurationH {
				t.Errorf("walk_windows.max_duration_h max = %v", p.Maximum)
			}
			if p := s.Properties["max_ascent_m"]; p.Maximum == nil || *p.Maximum != connector.MaxWalkAscentM {
				t.Errorf("walk_windows.max_ascent_m max = %v", p.Maximum)
			}
		}
		c := connector.Coverage
		for name, b := range map[string][2]float64{"lat": {c.MinLat, c.MaxLat}, "lon": {c.MinLon, c.MaxLon}} {
			p := s.Properties[name]
			if p.Minimum == nil || p.Maximum == nil || *p.Minimum != b[0] || *p.Maximum != b[1] {
				t.Errorf("%s.%s = min %v max %v, coverage %v", tool.Name, name, p.Minimum, p.Maximum, b)
			}
		}
		if d := s.Properties["detail"]; strings.Join(d.Enum, ",") != connector.DetailSummary+","+connector.DetailHours || d.Default != connector.DetailSummary {
			t.Errorf("%s detail = %+v", tool.Name, d)
		}
		switch tool.Name {
		case "dark_sky_windows":
			if strings.Join(s.Required, ",") != "lat,lon" {
				t.Errorf("%s required = %v", tool.Name, s.Required)
			}
		case "walk_windows":
			if len(s.Required) != 0 || len(s.AnyOf) != 2 || strings.Join(s.AnyOf[0].Required, ",") != "lat,lon" || strings.Join(s.AnyOf[1].Required, ",") != "walk_name" {
				t.Errorf("%s needs lat+lon or walk_name: %+v %+v", tool.Name, s.Required, s.AnyOf)
			}
			if p := s.Properties["walk_name"]; p.MaxLength == nil || *p.MaxLength != connector.MaxNameLength {
				t.Errorf("walk_name maxLength = %v", p.MaxLength)
			}
		}
	}
}

func TestModernToolCall(t *testing.T) {
	code, hdr, r := post(t, handler(),
		`{"jsonrpc":"2.0","id":"a","method":"tools/call","params":{"name":"dark_sky_windows","arguments":{"lat":57.19,"lon":-3.83,"nights":2},`+modernMeta+`}}`,
		modern("tools/call", "dark_sky_windows"))
	if code != http.StatusOK || r.Error != nil || string(r.ID) != `"a"` {
		t.Fatalf("status %d error %+v", code, r.Error)
	}
	if hdr.Get("MCP-Protocol-Version") != ModernVersion || hdr.Get("Mcp-Session-Id") != "" {
		t.Fatalf("headers %v", hdr)
	}
	if r.Result["isError"] != false || r.Result["resultType"] != "complete" {
		t.Fatalf("result %v", r.Result)
	}
	sc := r.Result["structuredContent"].(map[string]any)
	data := sc["data"].(map[string]any)
	if len(data["sites"].([]any)) != 3 || sc["meta"].(map[string]any)["api_version"] != "v1" {
		t.Fatalf("structuredContent %v", sc)
	}
	text := r.Result["content"].([]any)[0].(map[string]any)["text"].(string)
	var echoed map[string]any
	if err := json.Unmarshal([]byte(text), &echoed); err != nil || echoed["data"] == nil {
		t.Fatalf("text content is not the serialized envelope: %v", err)
	}
}

func TestWalkToolCall(t *testing.T) {
	code, _, r := post(t, handler(),
		`{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"walk_windows","arguments":{"lat":57.19,"lon":-3.83,"days":2,"max_ascent_m":600},`+modernMeta+`}}`,
		modern("tools/call", "walk_windows"))
	if code != http.StatusOK || r.Error != nil || r.Result["isError"] != false {
		t.Fatalf("status %d error %+v result %v", code, r.Error, r.Result)
	}
	data := r.Result["structuredContent"].(map[string]any)["data"].(map[string]any)
	ws := data["walks"].([]any)
	if len(ws) != 1 || !strings.HasPrefix(ws[0].(map[string]any)["url"].(string), "https://www.walkhighlands.co.uk/") {
		t.Fatalf("walks %v", ws)
	}
}

func TestToolErrorsAreResults(t *testing.T) {
	h := handler()
	for _, args := range []string{`{"limit":50,"lat":57.19,"lon":-3.83}`, `{"place":"Aviemore"}`, `{"lat":57.19}`, `{"lat":51.5,"lon":-0.1}`} {
		code, _, r := post(t, h,
			`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"dark_sky_windows","arguments":`+args+`,`+modernMeta+`}}`,
			modern("tools/call", "dark_sky_windows"))
		if code != http.StatusOK || r.Error != nil || r.Result["isError"] != true {
			t.Errorf("args %s: status %d error %+v result %v", args, code, r.Error, r.Result)
		}
	}
	code, _, r := post(t, h,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"aurora_windows","arguments":{},`+modernMeta+`}}`,
		modern("tools/call", "aurora_windows"))
	if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != codeInvalidParams {
		t.Fatalf("unknown tool: status %d error %+v", code, r.Error)
	}
}

func TestModernHeaderValidation(t *testing.T) {
	h := handler()
	call := `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"dark_sky_windows","arguments":{"lat":56.41,"lon":-5.47},` + modernMeta + `}}`
	cases := map[string]struct {
		hdr  map[string]string
		code int
	}{
		"missing version header": {map[string]string{"Mcp-Method": "tools/call", "Mcp-Name": "dark_sky_windows"}, codeHeaderMismatch},
		"method mismatch":        {modern("tools/list", "dark_sky_windows"), codeHeaderMismatch},
		"name mismatch":          {modern("tools/call", "aurora_windows"), codeHeaderMismatch},
		"missing name":           {modern("tools/call", ""), codeHeaderMismatch},
		"version mismatch":       {map[string]string{"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/call", "Mcp-Name": "dark_sky_windows"}, codeHeaderMismatch},
	}
	for name, c := range cases {
		code, _, r := post(t, h, call, c.hdr)
		if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != c.code {
			t.Errorf("%s: status %d error %+v", name, code, r.Error)
		}
	}
	// Base64-sentinel Mcp-Name decodes before comparison.
	if code, _, r := post(t, h, call, modern("tools/call", "=?base64?ZGFya19za3lfd2luZG93cw==?=")); code != http.StatusOK || r.Error != nil {
		t.Errorf("base64 name: status %d error %+v", code, r.Error)
	}
}

func TestUnsupportedVersionAndDiscover(t *testing.T) {
	h := handler()
	body := `{"jsonrpc":"2.0","id":5,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2099-01-01","io.modelcontextprotocol/clientCapabilities":{}}}}`
	code, _, r := post(t, h, body, map[string]string{"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"})
	if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != codeUnsupportedProtocol {
		t.Fatalf("status %d error %+v", code, r.Error)
	}
	data := r.Error.Data.(map[string]any)
	if data["requested"] != "2099-01-01" || data["supported"].([]any)[0] != ModernVersion {
		t.Fatalf("error data %v", data)
	}

	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":6,"method":"server/discover","params":{`+modernMeta+`}}`, modern("server/discover", ""))
	if code != http.StatusOK || r.Result["supportedVersions"].([]any)[0] != ModernVersion || r.Result["capabilities"] == nil {
		t.Fatalf("discover: status %d result %v", code, r.Result)
	}

	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":7,"method":"resources/list","params":{`+modernMeta+`}}`, modern("resources/list", ""))
	if code != http.StatusNotFound || r.Error == nil || r.Error.Code != codeMethodNotFound {
		t.Fatalf("unknown method: status %d error %+v", code, r.Error)
	}

	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":8,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}`, modern("tools/list", ""))
	if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != codeInvalidParams {
		t.Fatalf("missing capabilities: status %d error %+v", code, r.Error)
	}
}

// A 2025-era client: initialize, the initialized notification, then calls
// with only the version header. No session is minted.
func TestLegacyClient(t *testing.T) {
	h := handler()
	code, hdr, r := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"legacy","version":"1"}}}`, nil)
	if code != http.StatusOK || r.Result["protocolVersion"] != "2025-06-18" || hdr.Get("Mcp-Session-Id") != "" {
		t.Fatalf("initialize: status %d result %v headers %v", code, r.Result, hdr)
	}
	if _, ok := r.Result["capabilities"].(map[string]any)["tools"]; !ok {
		t.Fatalf("initialize capabilities %v", r.Result["capabilities"])
	}
	if code, _, _ := post(t, h, `{"jsonrpc":"2.0","method":"notifications/initialized"}`, map[string]string{"MCP-Protocol-Version": "2025-06-18"}); code != http.StatusAccepted {
		t.Fatalf("notification status %d", code)
	}
	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`, map[string]string{"MCP-Protocol-Version": "2025-06-18"})
	if code != http.StatusOK || len(r.Result["tools"].([]any)) != len(Tools) || r.Result["resultType"] != nil {
		t.Fatalf("legacy tools/list: status %d result %v", code, r.Result)
	}
	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"dark_sky_windows","arguments":{"lat":56.41,"lon":-5.47}}}`, map[string]string{"MCP-Protocol-Version": "2025-11-25"})
	if code != http.StatusOK || r.Result["isError"] != false {
		t.Fatalf("legacy tools/call: status %d result %v", code, r.Result)
	}
	// An unknown future version on initialize negotiates down to the newest legacy one.
	_, _, r = post(t, h, `{"jsonrpc":"2.0","id":4,"method":"initialize","params":{"protocolVersion":"2030-01-01","capabilities":{}}}`, nil)
	if r.Result["protocolVersion"] != LegacyVersions[0] {
		t.Fatalf("initialize fallback = %v", r.Result["protocolVersion"])
	}
}

func TestTransportGuards(t *testing.T) {
	h := handler()
	for _, method := range []string{http.MethodGet, http.MethodDelete} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(method, "/mcp", nil))
		if rec.Code != http.StatusMethodNotAllowed {
			t.Errorf("%s: status %d", method, rec.Code)
		}
	}
	if code, _, _ := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"Origin": "https://evil.example"}); code != http.StatusForbidden {
		t.Errorf("bad origin: status %d", code)
	}
	if code, _, _ := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"Origin": "https://claude.ai"}); code != http.StatusOK {
		t.Errorf("allowed origin: status %d", code)
	}
	if code, _, r := post(t, h, `[{"jsonrpc":"2.0","id":1,"method":"tools/list"}]`, nil); code != http.StatusBadRequest || r.Error.Code != codeInvalidRequest {
		t.Errorf("batch: status %d", code)
	}
	if code, _, r := post(t, h, `{not json`, nil); code != http.StatusBadRequest || r.Error.Code != codeParse || string(r.ID) != "null" {
		t.Errorf("parse error: status %d id %s", code, r.ID)
	}
	for _, id := range []string{`null`, `true`, `{}`, `[1]`} {
		code, _, r := post(t, h, `{"jsonrpc":"2.0","id":`+id+`,"method":"tools/list"}`, nil)
		if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != codeInvalidRequest || string(r.ID) != "null" {
			t.Errorf("id %s: status %d reply %+v", id, code, r)
		}
	}
	if code, _, r := post(t, h, `{"jsonrpc":"2.0","id":-7,"method":"ping"}`, nil); code != http.StatusOK || string(r.ID) != "-7" {
		t.Errorf("numeric id: status %d id %s", code, r.ID)
	}
	if code, _, _ := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"x","params":{"pad":"`+strings.Repeat("a", maxBody)+`"}}`, nil); code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized: status %d", code)
	}
}
