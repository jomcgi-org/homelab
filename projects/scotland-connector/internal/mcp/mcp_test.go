package mcp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/prometheus/client_golang/prometheus"
)

func handler() *Handler {
	return New(fixture.Service(true), meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)),
		ServerInfo{Name: "scotland-connector", Version: "test"}, []string{"https://claude.ai"})
}

// auroraStore is an enabled aurora store whose snapshot has not loaded.
type auroraStore struct{}

func (auroraStore) Current() *aurora.Snapshot { return nil }

func auroraHandler() *Handler {
	svc := fixture.Service(true)
	svc.AuroraData = auroraStore{}
	return New(svc, meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)),
		ServerInfo{Name: "scotland-connector", Version: "test"}, []string{"https://claude.ai"})
}

// aurora is listed, and named in the instructions, only when its store is
// enabled: a disabled one answers every call unknown. A client holding an
// older list can still call it.
func TestAuroraListedOnlyWhenEnabled(t *testing.T) {
	names := func(h *Handler) (out []string, instructions string) {
		t.Helper()
		_, _, r := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`, map[string]string{"MCP-Protocol-Version": "2025-06-18"})
		for _, tool := range r.Result["tools"].([]any) {
			out = append(out, tool.(map[string]any)["name"].(string))
		}
		_, _, r = post(t, h, `{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}`, nil)
		return out, r.Result["instructions"].(string)
	}
	off, offText := names(handler())
	if strings.Join(off, ",") != "light,plan,check" || strings.Contains(offText, "aurora") {
		t.Errorf("disabled: tools %v, instructions %q", off, offText)
	}
	on, onText := names(auroraHandler())
	if strings.Join(on, ",") != "light,aurora,plan,check" || onText != Instructions {
		t.Errorf("enabled: tools %v, instructions %q", on, onText)
	}
	code, _, r := post(t, handler(), `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"aurora","arguments":{"lat":58.44,"lon":-3.09,"at":"2026-06-15T22:00:00Z"}}}`, map[string]string{"MCP-Protocol-Version": "2025-06-18"})
	if code != http.StatusOK || r.Result["isError"] != false {
		t.Fatalf("aurora call while unlisted: status %d result %v", code, r.Result)
	}
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
	code, _, r := post(t, auroraHandler(), `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{`+modernMeta+`}}`, modern("tools/list", ""))
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
	if strings.Join(names, ",") != "light,aurora,plan,check" {
		t.Fatalf("tools = %v", names)
	}
}

// The schemas carry the code's caps, and no combinator at the root: the
// Messages API rejects anyOf, oneOf and allOf there.
func TestToolSchemaCapsMatchCode(t *testing.T) {
	type prop struct {
		Type        string   `json:"type"`
		Minimum     *float64 `json:"minimum"`
		Maximum     *float64 `json:"maximum"`
		Default     any      `json:"default"`
		MaxLength   *int     `json:"maxLength"`
		MinItems    *int     `json:"minItems"`
		MaxItems    *int     `json:"maxItems"`
		Enum        []string `json:"enum"`
		Description string   `json:"description"`
		Items       *struct {
			Enum       []string        `json:"enum"`
			Properties map[string]prop `json:"properties"`
			Required   []string        `json:"required"`
		} `json:"items"`
	}
	type schema struct {
		Properties map[string]prop `json:"properties"`
		Required   []string        `json:"required"`
		AnyOf      []any           `json:"anyOf"`
		OneOf      []any           `json:"oneOf"`
		AllOf      []any           `json:"allOf"`
	}
	schemas := map[string]schema{}
	for _, tool := range Tools {
		var s schema
		if err := json.Unmarshal(tool.InputSchema, &s); err != nil {
			t.Fatalf("%s: %v", tool.Name, err)
		}
		if s.AnyOf != nil || s.OneOf != nil || s.AllOf != nil {
			t.Errorf("%s: root combinator", tool.Name)
		}
		schemas[tool.Name] = s
	}
	bounds := func(where string, p prop, lo, hi float64) {
		t.Helper()
		if p.Minimum == nil || p.Maximum == nil || *p.Minimum != lo || *p.Maximum != hi {
			t.Errorf("%s = min %v max %v, code %v..%v", where, p.Minimum, p.Maximum, lo, hi)
		}
	}
	defaults := func(where string, p prop, want float64) {
		t.Helper()
		if d, ok := p.Default.(float64); !ok || d != want {
			t.Errorf("%s default = %v, code %v", where, p.Default, want)
		}
	}
	c := connector.Coverage
	activities := connector.ActivityWalk + "," + connector.ActivityStargaze

	plan := schemas["plan"]
	if strings.Join(plan.Required, ",") != "lat,lon" {
		t.Errorf("plan required = %v", plan.Required)
	}
	bounds("plan.lat", plan.Properties["lat"], c.MinLat, c.MaxLat)
	bounds("plan.lon", plan.Properties["lon"], c.MinLon, c.MaxLon)
	bounds("plan.radius_km", plan.Properties["radius_km"], 1, connector.MaxRadiusKm)
	defaults("plan.radius_km", plan.Properties["radius_km"], connector.DefaultRadiusKm)
	bounds("plan.limit", plan.Properties["limit"], 1, connector.MaxOptions)
	defaults("plan.limit", plan.Properties["limit"], connector.DefaultOptions)
	bounds("plan.max_duration_h", plan.Properties["max_duration_h"], 0.5, connector.MaxDurationH)
	bounds("plan.min_duration_h", plan.Properties["min_duration_h"], 0.5, connector.MaxDurationH)
	bounds("plan.min_length_km", plan.Properties["min_length_km"], 0.5, connector.MaxLengthKm)
	bounds("plan.max_length_km", plan.Properties["max_length_km"], 0.5, connector.MaxLengthKm)
	for name, props := range map[string]map[string]prop{"plan": plan.Properties, "check": schemas["check"].Properties} {
		bounds(name+".max_rain_mm_h", props["max_rain_mm_h"], 0, connector.MaxRainLimitMM)
		bounds(name+".max_wind_kmh", props["max_wind_kmh"], 0, connector.MaxWindLimitKmh)
		bounds(name+".min_temp_c", props["min_temp_c"], connector.MinTempLimitC, connector.MaxTempLimitC)
	}
	bounds("plan.max_ascent_m", plan.Properties["max_ascent_m"], 1, connector.MaxAscentM)
	if d := plan.Properties["from"].Description; !strings.Contains(d, fmt.Sprintf("up to %d days ahead", connector.MaxDaysAhead)) {
		t.Errorf("plan.from description %q does not match the code's window", d)
	}
	if d := plan.Properties["to"].Description; !strings.Contains(d, fmt.Sprintf("at most %d days", connector.MaxSpanDays)) {
		t.Errorf("plan.to description %q does not match the code's span", d)
	}
	if it := plan.Properties["activities"].Items; it == nil || strings.Join(it.Enum, ",") != activities {
		t.Errorf("plan.activities = %+v", it)
	}

	check := schemas["check"]
	items := check.Properties["items"]
	if strings.Join(check.Required, ",") != "items" || items.MinItems == nil || *items.MinItems != 1 || items.MaxItems == nil || *items.MaxItems != connector.MaxCheckItems || items.Items == nil {
		t.Fatalf("check.items = %+v", items)
	}
	item := items.Items
	if strings.Join(item.Required, ",") != "activity,date" || strings.Join(item.Properties["activity"].Enum, ",") != activities {
		t.Errorf("check item = %+v", item)
	}
	bounds("check.lat", item.Properties["lat"], c.MinLat, c.MaxLat)
	bounds("check.lon", item.Properties["lon"], c.MinLon, c.MaxLon)
	if p := item.Properties["walk_name"]; p.MaxLength == nil || *p.MaxLength != connector.MaxNameLength {
		t.Errorf("check.walk_name maxLength = %v", p.MaxLength)
	}
	if d := item.Properties["date"].Description; !strings.Contains(d, fmt.Sprintf("up to %d days ahead", connector.MaxDaysAhead)) {
		t.Errorf("check.date description %q does not match the code's window", d)
	}
}

func callTool(t *testing.T, h http.Handler, name, args string) reply {
	t.Helper()
	code, hdr, r := post(t, h,
		`{"jsonrpc":"2.0","id":"a","method":"tools/call","params":{"name":"`+name+`","arguments":`+args+`,`+modernMeta+`}}`,
		modern("tools/call", name))
	if code != http.StatusOK || r.Error != nil || string(r.ID) != `"a"` {
		t.Fatalf("%s %s: status %d error %+v", name, args, code, r.Error)
	}
	if hdr.Get("MCP-Protocol-Version") != ModernVersion || hdr.Get("Mcp-Session-Id") != "" {
		t.Fatalf("headers %v", hdr)
	}
	return r
}

func TestPlanToolCall(t *testing.T) {
	r := callTool(t, handler(), "plan", `{"lat":57.19,"lon":-3.83,"from":"2026-10-20","to":"2026-10-20"}`)
	if r.Result["isError"] != false || r.Result["resultType"] != "complete" {
		t.Fatalf("result %v", r.Result)
	}
	sc := r.Result["structuredContent"].(map[string]any)
	days := sc["data"].(map[string]any)["days"].([]any)
	if len(days) != 1 || sc["meta"].(map[string]any)["api_version"] != "v1" {
		t.Fatalf("structuredContent %v", sc)
	}
	walk := days[0].(map[string]any)["walks"].([]any)[0].(map[string]any)
	if walk["verdict"] != "go" || !strings.HasPrefix(walk["url"].(string), "https://www.walkhighlands.co.uk/") {
		t.Fatalf("walk %v", walk)
	}
	text := r.Result["content"].([]any)[0].(map[string]any)["text"].(string)
	var echoed map[string]any
	if err := json.Unmarshal([]byte(text), &echoed); err != nil || echoed["data"] == nil {
		t.Fatalf("text content is not the serialized envelope: %v", err)
	}
}

func TestCheckToolCall(t *testing.T) {
	r := callTool(t, handler(), "check", `{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"Cairn Gorm"},{"activity":"stargaze","date":"2026-10-19","lat":57.3,"lon":-3.7}]}`)
	if r.Result["isError"] != false {
		t.Fatalf("result %v", r.Result)
	}
	items := r.Result["structuredContent"].(map[string]any)["data"].(map[string]any)["items"].([]any)
	if len(items) != 2 || items[0].(map[string]any)["verdict"] != "no" || items[1].(map[string]any)["verdict"] != "marginal" {
		t.Fatalf("items %v", items)
	}
}

func TestLightToolCall(t *testing.T) {
	r := callTool(t, handler(), "light", fixture.LightArgs)
	if r.Error != nil || r.Result["isError"] != false {
		t.Fatal(r)
	}
	env, err := fixture.Service(true).Light(fixture.LightParams())
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(env)
	var want map[string]any
	if err := json.Unmarshal(b, &want); err != nil {
		t.Fatal(err)
	}
	wantJSON, _ := json.Marshal(want)
	gotJSON, _ := json.Marshal(r.Result["structuredContent"])
	if string(wantJSON) != string(gotJSON) {
		t.Fatal("MCP differs from shared service")
	}
	var tool Tool
	for _, candidate := range Tools {
		if candidate.Name == "light" {
			tool = candidate
		}
	}
	var schema struct {
		Properties map[string]struct{ Minimum, Maximum, Default float64 }
		Required   []string
	}
	if err := json.Unmarshal(tool.InputSchema, &schema); err != nil {
		t.Fatal(err)
	}
	if strings.Join(schema.Required, ",") != "lat,lon" || schema.Properties["radius_km"].Maximum != connector.MaxRadiusKm || schema.Properties["radius_km"].Default != connector.DefaultRadiusKm {
		t.Fatal(schema)
	}
	for _, args := range []string{`{}`, `{"lat":57.3}`, `{"lat":57.3,"lon":-3.7,"to":"2026-10-26"}`, `{"lat":57.3,"lon":-3.7,"extra":1}`} {
		r := callTool(t, handler(), "light", args)
		if r.Result["isError"] != true {
			t.Fatal(args, r)
		}
	}
}

func TestToolErrorsAreResults(t *testing.T) {
	h := handler()
	for name, args := range map[string][]string{
		"plan":  {`{"limit":50,"lat":57.19,"lon":-3.83}`, `{"place":"Aviemore"}`, `{"lat":57.19}`, `{"lat":51.5,"lon":-0.1}`, `{"lat":57.19,"lon":-3.83,"activities":["ski"]}`},
		"check": {`{}`, `{"items":[]}`, `{"items":[{"activity":"walk","date":"2026-10-20"}]}`, `{"items":[{"activity":"stargaze","date":"tomorrow","lat":57.3,"lon":-3.7}]}`},
	} {
		for _, a := range args {
			code, _, r := post(t, h,
				`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"`+name+`","arguments":`+a+`,`+modernMeta+`}}`,
				modern("tools/call", name))
			if code != http.StatusOK || r.Error != nil || r.Result["isError"] != true {
				t.Errorf("%s %s: status %d error %+v result %v", name, a, code, r.Error, r.Result)
			}
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
	call := `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"plan","arguments":{"lat":56.41,"lon":-5.47},` + modernMeta + `}}`
	cases := map[string]struct {
		hdr  map[string]string
		code int
	}{
		"missing version header": {map[string]string{"Mcp-Method": "tools/call", "Mcp-Name": "plan"}, codeHeaderMismatch},
		"method mismatch":        {modern("tools/list", "plan"), codeHeaderMismatch},
		"name mismatch":          {modern("tools/call", "aurora_windows"), codeHeaderMismatch},
		"missing name":           {modern("tools/call", ""), codeHeaderMismatch},
		"version mismatch":       {map[string]string{"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/call", "Mcp-Name": "plan"}, codeHeaderMismatch},
	}
	for name, c := range cases {
		code, _, r := post(t, h, call, c.hdr)
		if code != http.StatusBadRequest || r.Error == nil || r.Error.Code != c.code {
			t.Errorf("%s: status %d error %+v", name, code, r.Error)
		}
	}
	// Base64-sentinel Mcp-Name decodes before comparison.
	if code, _, r := post(t, h, call, modern("tools/call", "=?base64?cGxhbg==?=")); code != http.StatusOK || r.Error != nil {
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
	if code != http.StatusOK || len(r.Result["tools"].([]any)) != len(h.tools()) || r.Result["resultType"] != nil {
		t.Fatalf("legacy tools/list: status %d result %v", code, r.Result)
	}
	code, _, r = post(t, h, `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"plan","arguments":{"lat":56.41,"lon":-5.47}}}`, map[string]string{"MCP-Protocol-Version": "2025-11-25"})
	if code != http.StatusOK || r.Result["isError"] != false {
		t.Fatalf("legacy tools/call: status %d result %v", code, r.Result)
	}
	// An unknown future version on initialize negotiates down to the newest legacy one.
	_, _, r = post(t, h, `{"jsonrpc":"2.0","id":4,"method":"initialize","params":{"protocolVersion":"2030-01-01","capabilities":{}}}`, nil)
	if r.Result["protocolVersion"] != LegacyVersions[0] {
		t.Fatalf("initialize fallback = %v", r.Result["protocolVersion"])
	}
}

func TestDiscoveryInstructionsIncludeLightScope(t *testing.T) {
	for _, tc := range []struct {
		method  string
		body    string
		headers map[string]string
	}{
		{"initialize", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}`, nil},
		{"server/discover", `{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{` + modernMeta + `}}`, modern("server/discover", "")},
	} {
		t.Run(tc.method, func(t *testing.T) {
			code, _, r := post(t, auroraHandler(), tc.body, tc.headers)
			if code != http.StatusOK || r.Error != nil {
				t.Fatalf("status %d error %+v", code, r.Error)
			}
			instructions, ok := r.Result["instructions"].(string)
			if !ok || instructions != Instructions {
				t.Fatalf("instructions missing or inconsistent: %v", r.Result["instructions"])
			}
			for _, guidance := range []string{"Use light", "no top-level verdict", "cloud", "nearby-site hourly sample support", "nights", "interval cloud forecasts", "For plan, check and aurora", "meta.sources"} {
				if !strings.Contains(instructions, guidance) {
					t.Errorf("discovery instructions omit %q", guidance)
				}
			}
			if strings.Contains(instructions, "Lead with each answer's verdict") {
				t.Error("discovery requires a verdict from light")
			}
		})
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

func TestNamedProducerBytesAcrossBothTransports(t *testing.T) {
	svc := fixture.NamedService()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	met := meter.New(prometheus.NewRegistry())
	rest, err := httpapi.New(svc, met, logger, "")
	if err != nil {
		t.Fatal(err)
	}
	mcp := New(svc, met, logger, ServerInfo{Name: "test", Version: "test"}, nil)
	tests := []struct{ name, args, url string }{
		{"plan", `{"lat":57.3,"lon":-3.7,"from":"2026-10-19","to":"2026-10-19","activities":["stargaze"]}`, "/v1/plan?lat=57.3&lon=-3.7&from=2026-10-19&to=2026-10-19&activities=stargaze"},
		{"check", `{"items":[{"activity":"stargaze","date":"2026-10-19","lat":57.3,"lon":-3.7}]}`, "/v1/check"},
		{"light", `{"lat":57.3,"lon":-3.7,"from":"2026-10-19","to":"2026-10-19"}`, "/v1/light?lat=57.3&lon=-3.7&from=2026-10-19&to=2026-10-19"},
	}
	ds := svc.Data.Current()
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, tc.url, nil)
			if tc.name == "check" {
				req = httptest.NewRequest(http.MethodPost, tc.url, strings.NewReader(tc.args))
				req.Header.Set("Content-Type", "application/json")
			}
			rec := httptest.NewRecorder()
			rest.ServeHTTP(rec, req)
			if rec.Code != 200 {
				t.Fatal(rec.Code, rec.Body.String())
			}
			var httpBody map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &httpBody); err != nil {
				t.Fatal(err)
			}
			result := callTool(t, mcp, tc.name, tc.args)
			if result.Result["isError"] != false {
				t.Fatal(result)
			}
			structured := result.Result["structuredContent"]
			if !reflect.DeepEqual(httpBody, structured) {
				t.Fatal("HTTP/MCP envelope mismatch", httpBody, structured)
			}
			text := result.Result["content"].([]any)[0].(map[string]any)["text"].(string)
			var decoded any
			if err := json.Unmarshal([]byte(text), &decoded); err != nil || !reflect.DeepEqual(decoded, structured) {
				t.Fatal("MCP text differs", text)
			}
			var sites []map[string]any
			data := httpBody["data"].(map[string]any)
			switch tc.name {
			case "plan":
				for _, day := range data["days"].([]any) {
					for _, site := range day.(map[string]any)["stargazing"].([]any) {
						sites = append(sites, site.(map[string]any))
					}
				}
			case "check":
				sites = append(sites, data["items"].([]any)[0].(map[string]any)["night"].(map[string]any))
			case "light":
				sites = append(sites, data["site"].(map[string]any))
			}
			if len(sites) == 0 {
				t.Fatal("no public sites")
			}
			for _, site := range sites {
				near := site["nearby_place"].(map[string]any)
				expectedID := "site-00000"
				if tc.name == "light" {
					expectedID = "empty"
				} // existing nearest-ID tie-break
				if site["site_id"] != expectedID || site["lat"] != 57.3 || site["lon"] != -3.7 || near["label"] != "Near Aviemore" || near["distance_km"] != 14.354 || near["source_version"] != "2026-10" {
					t.Fatal(site)
				}
				if _, ok := site["altitude_m"]; ok {
					t.Fatal("unsupported altitude", site)
				}
			}
			sources := httpBody["meta"].(map[string]any)["sources"].([]any)
			found := false
			for _, src := range sources {
				v := src.(map[string]any)
				if v["name"] == "OS Open Names" {
					found = true
					if v["license"] != "Open Government Licence v3.0" || v["attribution"] != "Contains OS data © Crown Copyright and database rights 2026." {
						t.Fatal(v)
					}
				}
			}
			if !found {
				t.Fatal("missing attribution")
			}
			// Remove only naming and use a different snapshot version. Every data byte
			// besides nullable associations must remain identical, including uncertainty.
			var raw map[string]any
			_ = json.Unmarshal(fixture.NamedSites, &raw)
			for _, site := range raw["sites"].([]any) {
				delete(site.(map[string]any), "nearby_place")
			}
			oldBytes, _ := json.Marshal(raw)
			old, err := stars.Parse(oldBytes, []byte(`{"sites":[]}`), "old-snapshot")
			if err != nil {
				t.Fatal(err)
			}
			svc.Data = fixture.Source{DS: old}
			plain := callTool(t, mcp, tc.name, tc.args).Result["structuredContent"].(map[string]any)
			for _, site := range sites {
				site["nearby_place"] = nil
			}
			a, _ := json.Marshal(httpBody["data"])
			b, _ := json.Marshal(plain["data"])
			if !bytes.Equal(a, b) {
				t.Fatal("naming changed answer data", string(a), string(b))
			}
			if plain["meta"].(map[string]any)["data_version"] == httpBody["meta"].(map[string]any)["data_version"] {
				t.Fatal("snapshot cache identity lost")
			}
			svc.Data = fixture.Source{DS: ds}
		})
	}
}
