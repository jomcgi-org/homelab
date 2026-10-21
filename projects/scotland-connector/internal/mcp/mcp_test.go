package mcp

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
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
	if strings.Join(names, ",") != "plan,check" {
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
		bounds(name+".max_rain_mm_h", props["max_rain_mm_h"], 0, walks.ViableMaxPrecipMM)
		bounds(name+".max_wind_kmh", props["max_wind_kmh"], 0, walks.ViableMaxWindKmh)
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
	if code != http.StatusOK || len(r.Result["tools"].([]any)) != len(Tools) || r.Result["resultType"] != nil {
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
