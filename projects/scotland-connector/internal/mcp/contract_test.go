package mcp

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
)

// Compare every advertised node with the checked-in component, following
// references in the source rather than using the production expansion helper.
func compareComponent(t *testing.T, got, want any, schemas map[string]any) {
	t.Helper()
	switch w := want.(type) {
	case map[string]any:
		if ref, ok := w["$ref"].(string); ok {
			merged := map[string]any{}
			for k, v := range schemas[strings.TrimPrefix(ref, "#/components/schemas/")].(map[string]any) {
				merged[k] = v
			}
			for k, v := range w {
				if k != "$ref" {
					merged[k] = v
				}
			}
			compareComponent(t, got, merged, schemas)
			return
		}
		g, ok := got.(map[string]any)
		if !ok || len(g) != len(w) {
			t.Fatalf("schema object differs: got %v want %v", got, want)
		}
		for k, v := range w {
			compareComponent(t, g[k], v, schemas)
		}
	case []any:
		g, ok := got.([]any)
		if !ok || len(g) != len(w) {
			t.Fatalf("schema array differs: %v vs %v", got, want)
		}
		for i, v := range w {
			compareComponent(t, g[i], v, schemas)
		}
	default:
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("schema differs: %v vs %v", got, want)
		}
	}
}

// Validate the JSON Schema assertions used by these response components.
// Format is an annotation under JSON Schema 2020-12, not an assertion.
func conforms(value any, s map[string]any) error {
	fail := func() error { return fmt.Errorf("value %v does not conform to %v", value, s) }
	if _, ok := s["$ref"]; ok {
		return fmt.Errorf("unresolved reference")
	}
	if choices, ok := s["oneOf"].([]any); ok {
		matches := 0
		for _, c := range choices {
			if conforms(value, c.(map[string]any)) == nil {
				matches++
			}
		}
		if matches != 1 {
			return fail()
		}
	}
	if parts, ok := s["allOf"].([]any); ok {
		for _, c := range parts {
			if err := conforms(value, c.(map[string]any)); err != nil {
				return err
			}
		}
	}
	if typ, ok := s["type"]; ok {
		types, ok := typ.([]any)
		if !ok {
			types = []any{typ}
		}
		matches := false
		for _, typ := range types {
			switch typ {
			case "null":
				matches = matches || value == nil
			case "object":
				_, ok := value.(map[string]any)
				matches = matches || ok
			case "array":
				_, ok := value.([]any)
				matches = matches || ok
			case "string":
				_, ok := value.(string)
				matches = matches || ok
			case "boolean":
				_, ok := value.(bool)
				matches = matches || ok
			case "number":
				_, ok := value.(float64)
				matches = matches || ok
			case "integer":
				n, ok := value.(float64)
				matches = matches || ok && math.Trunc(n) == n
			default:
				return fmt.Errorf("unsupported type %v", typ)
			}
		}
		if !matches {
			return fail()
		}
	}
	if c, ok := s["const"]; ok && !reflect.DeepEqual(c, value) {
		return fail()
	}
	if enum, ok := s["enum"].([]any); ok {
		found := false
		for _, c := range enum {
			found = found || reflect.DeepEqual(c, value)
		}
		if !found {
			return fail()
		}
	}
	if value == nil {
		return nil
	}
	switch v := value.(type) {
	case map[string]any:
		if required, ok := s["required"].([]any); ok {
			for _, key := range required {
				if _, ok := v[key.(string)]; !ok {
					return fail()
				}
			}
		}
		props, _ := s["properties"].(map[string]any)
		for k, val := range v {
			child, ok := props[k].(map[string]any)
			if !ok {
				if schema, ok := s["additionalProperties"].(map[string]any); ok {
					if err := conforms(val, schema); err != nil {
						return err
					}
				}
				if s["additionalProperties"] == false {
					return fail()
				}
				continue
			}
			if err := conforms(val, child); err != nil {
				return fmt.Errorf("%s: %w", k, err)
			}
		}
	case []any:
		if n, ok := s["minItems"].(float64); ok && len(v) < int(n) {
			return fail()
		}
		if n, ok := s["maxItems"].(float64); ok && len(v) > int(n) {
			return fail()
		}
		for i, val := range v {
			if s["uniqueItems"] == true {
				for _, prev := range v[:i] {
					if reflect.DeepEqual(prev, val) {
						return fail()
					}
				}
			}
			if item, ok := s["items"].(map[string]any); ok {
				if err := conforms(val, item); err != nil {
					return err
				}
			}
		}
	case float64:
		if n, ok := s["minimum"].(float64); ok && v < n {
			return fail()
		}
		if n, ok := s["maximum"].(float64); ok && v > n {
			return fail()
		}
	case string:
		if pattern, ok := s["pattern"].(string); ok {
			if !regexp.MustCompile(pattern).MatchString(v) {
				return fail()
			}
		}
	}
	return nil
}

func TestLightMCPKindsContract(t *testing.T) {
	base := `{"lat":57.3,"lon":-3.7`
	all := callTool(t, handler(), "light", base+`}`)
	for _, value := range []string{`null`, `[]`, `["milky_way","darkness","astronomical","nautical","civil","blue","golden","golden"]`} {
		r := callTool(t, handler(), "light", base+`,"kinds":`+value+`}`)
		if r.Result["isError"] != false || !reflect.DeepEqual(r.Result["structuredContent"], all.Result["structuredContent"]) {
			t.Fatal("noncanonical MCP kinds", value, r)
		}
	}
	a := callTool(t, handler(), "light", base+`,"kinds":["blue","golden","blue"]}`)
	b := callTool(t, handler(), "light", base+`,"kinds":[" golden ","blue"]}`)
	if a.Result["isError"] != false || !reflect.DeepEqual(a.Result["structuredContent"], b.Result["structuredContent"]) {
		t.Fatal("MCP kinds differ")
	}
	for _, value := range []string{`"golden"`, `[""]`, `["golden",""]`, `["other"]`, `[42]`} {
		if r := callTool(t, handler(), "light", base+`,"kinds":`+value+`}`); r.Result["isError"] != true {
			t.Fatal("invalid MCP kinds", value)
		}
	}
}

func TestOutputSchemasMatchOpenAPIAndStructuredContent(t *testing.T) {
	spec, _ := httpapi.Documentation()
	var doc map[string]any
	if err := json.Unmarshal(spec, &doc); err != nil {
		t.Fatal(err)
	}
	schemas := doc["components"].(map[string]any)["schemas"].(map[string]any)
	_, _, listing := post(t, handler(), `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{`+modernMeta+`}}`, modern("tools/list", ""))
	if listing.Error != nil {
		t.Fatal(listing.Error)
	}
	listed := map[string]map[string]any{}
	for _, raw := range listing.Result["tools"].([]any) {
		tool := raw.(map[string]any)
		listed[tool["name"].(string)] = tool["outputSchema"].(map[string]any)
	}
	args := map[string][]string{
		"light":  {`{"lat":57.3,"lon":-3.7,"to":"2026-10-25"}`, `{"lat":55,"lon":-8,"kinds":["golden","blue"]}`},
		"plan":   {`{"lat":57.19,"lon":-3.83,"to":"2026-10-25"}`, `{"lat":55,"lon":-8}`},
		"check":  {`{"items":[{"activity":"walk","date":"2026-10-20","walk_id":"wh-meall-a-bhuachaille"},{"activity":"stargaze","date":"2026-10-19","lat":57.3,"lon":-3.7}]}`, `{"items":[{"activity":"walk","date":"2026-10-20","walk_id":"missing"}]}`},
		"aurora": {`{"lat":57.3,"lon":-3.7,"at":"2026-10-19T22:00:00Z"}`},
	}
	for _, tool := range Tools {
		t.Run(tool.Name, func(t *testing.T) {
			var advertised map[string]any
			if err := json.Unmarshal(tool.OutputSchema, &advertised); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(advertised, listed[tool.Name]) {
				t.Fatal("advertised schema differs from tools/list")
			}
			if strings.Contains(string(tool.OutputSchema), `"$ref"`) {
				t.Fatal("schema is not self-contained")
			}
			name := strings.ToUpper(tool.Name[:1]) + tool.Name[1:] + "Response"
			compareComponent(t, advertised, schemas[name], schemas)
			for _, arg := range args[tool.Name] {
				r := callTool(t, handler(), tool.Name, arg)
				if r.Error != nil || r.Result["isError"] != false {
					t.Fatal(r)
				}
				if err := conforms(r.Result["structuredContent"], advertised); err != nil {
					t.Fatal(err)
				}
				text := r.Result["content"].([]any)[0].(map[string]any)["text"].(string)
				var value any
				if err := json.Unmarshal([]byte(text), &value); err != nil {
					t.Fatal(err)
				}
				if !reflect.DeepEqual(value, r.Result["structuredContent"]) {
					t.Fatal("transport representations differ")
				}
			}
			if conforms(map[string]any{}, advertised) == nil || conforms(map[string]any{"data": 42.0, "meta": false}, advertised) == nil {
				t.Fatal("validator accepted invalid response")
			}
		})
	}
}

func TestPlanHorizonDescriptions(t *testing.T) {
	spec, _ := httpapi.Documentation()
	var doc map[string]any
	if err := json.Unmarshal(spec, &doc); err != nil {
		t.Fatal(err)
	}
	objectAt := func(t *testing.T, root map[string]any, path ...string) map[string]any {
		t.Helper()
		for _, key := range path {
			child, ok := root[key].(map[string]any)
			if !ok {
				t.Fatalf("missing object %q in %v", key, path)
			}
			root = child
		}
		return root
	}
	checkDescription := func(t *testing.T, schema map[string]any, phrases ...string) {
		t.Helper()
		description, _ := schema["description"].(string)
		for _, phrase := range phrases {
			if !strings.Contains(description, phrase) {
				t.Errorf("description %q does not explain %q", description, phrase)
			}
		}
	}
	t.Run("endpoint", func(t *testing.T) {
		checkDescription(t, objectAt(t, doc, "paths", "/v1/plan", "get"), "beyond the hourly forecast horizon", "once in datasets.<activity>.notes")
	})
	day := objectAt(t, doc, "components", "schemas", "PlanDay", "properties")
	t.Run("component", func(t *testing.T) {
		checkDescription(t, objectAt(t, day, "walk_note"), "once in datasets.walk.notes", "may be absent")
		checkDescription(t, objectAt(t, day, "stargazing_note"), "once in datasets.stargaze.notes", "only moon advice", "absent")
	})
	for _, version := range []string{ModernVersion, LegacyVersions[0]} {
		t.Run(version, func(t *testing.T) {
			params := ""
			headers := map[string]string{"MCP-Protocol-Version": version}
			if version == ModernVersion {
				params = modernMeta
				headers = modern("tools/list", "")
			}
			code, _, listing := post(t, handler(), `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{`+params+`}}`, headers)
			if code != http.StatusOK || listing.Error != nil {
				t.Fatalf("tools/list: status %d, error %v", code, listing.Error)
			}
			for _, raw := range listing.Result["tools"].([]any) {
				tool := raw.(map[string]any)
				if tool["name"] != "plan" {
					continue
				}
				checkDescription(t, tool, "beyond the hourly forecast horizon", "once in datasets.<activity>.notes")
				advertised := objectAt(t, tool, "outputSchema", "properties", "data", "properties", "days", "items", "properties")
				for _, name := range []string{"walk_note", "stargazing_note"} {
					if got, want := objectAt(t, advertised, name)["description"], objectAt(t, day, name)["description"]; got != want {
						t.Errorf("%s description: got %v, want %v", name, got, want)
					}
				}
				return
			}
			t.Fatal("plan is missing from tools/list")
		})
	}
}

type schemaAuroraSource struct{ snapshot *aurora.Snapshot }

func (s schemaAuroraSource) Current() *aurora.Snapshot { return s.snapshot }

func TestEnabledAuroraOutputSchema(t *testing.T) {
	in, err := fixture.AuroraInputs()
	if err != nil {
		t.Fatal(err)
	}
	body, err := aurora.Materialize(in)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := aurora.Parse(body)
	if err != nil {
		t.Fatal(err)
	}
	h := handler()
	h.svc.AuroraData = schemaAuroraSource{snapshot}
	h.svc.Now = func() time.Time { return fixture.AuroraNow }
	r := callTool(t, h, "aurora", `{"lat":58,"lon":-4,"at":"2026-12-01T23:00:00Z"}`)
	if r.Error != nil || r.Result["isError"] != false {
		t.Fatal(r)
	}
	var schema map[string]any
	if err := json.Unmarshal(toolOutputSchema("aurora"), &schema); err != nil {
		t.Fatal(err)
	}
	if err := conforms(r.Result["structuredContent"], schema); err != nil {
		t.Fatal(err)
	}
	data := r.Result["structuredContent"].(map[string]any)["data"].(map[string]any)
	if data["verdict"] != "go" {
		t.Fatal("enabled fixture must exercise supported evidence", data)
	}
}

func TestDocumentationCapabilitiesDispatchAndBytes(t *testing.T) {
	h := handler()
	spec, method := httpapi.Documentation()
	for _, version := range []string{ModernVersion, LegacyVersions[0]} {
		t.Run(version, func(t *testing.T) {
			request := func(method string, params string) reply {
				headers := map[string]string{"MCP-Protocol-Version": version}
				if version == ModernVersion {
					headers = modern(method, "")
					if params != "" {
						params += ","
					}
					params += modernMeta
				}
				_, _, r := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"`+method+`","params":{`+params+`}}`, headers)
				return r
			}
			discovery := "initialize"
			if version == ModernVersion {
				discovery = "server/discover"
			}
			r := request(discovery, "")
			caps := r.Result["capabilities"].(map[string]any)
			for _, name := range []string{"tools", "prompts", "resources"} {
				if caps[name] == nil {
					t.Fatal("missing capability", name)
				}
			}
			r = request("prompts/list", "")
			if r.Error != nil || len(r.Result["prompts"].([]any)) != 3 {
				t.Fatal(r)
			}
			for _, p := range prompts {
				arguments := map[string]string{}
				for _, a := range p.Arguments {
					arguments[a.Name] = "user value"
				}
				arguments["lat"] = "57.3"
				arguments["lon"] = "-3.7"
				b, _ := json.Marshal(arguments)
				r = request("prompts/get", `"name":"`+p.Name+`","arguments":`+string(b))
				if r.Error != nil {
					t.Fatal(r.Error)
				}
				message := r.Result["messages"].([]any)[0].(map[string]any)
				content := message["content"].(map[string]any)
				if message["role"] != "user" || content["type"] != "text" || !strings.Contains(content["text"].(string), "57.3") {
					t.Fatal(message)
				}
			}
			for _, p := range []string{`"name":"unknown"`, `"name":"plan_trip","arguments":{}`, `"name":"photo_light","arguments":{"lat":"57","lon":"-3","bad":"x"}`, `"name":"check_walk","arguments":{"lat":57}`} {
				if r := request("prompts/get", p); r.Error == nil || r.Error.Code != codeInvalidParams {
					t.Fatal(r)
				}
			}
			r = request("resources/list", "")
			if r.Error != nil || len(r.Result["resources"].([]any)) != 2 {
				t.Fatal(r)
			}
			for i, resource := range resources {
				r = request("resources/read", `"uri":"`+resource.URI+`"`)
				if r.Error != nil {
					t.Fatal(r.Error)
				}
				c := r.Result["contents"].([]any)[0].(map[string]any)
				want := method
				if i == 1 {
					want = spec
				}
				if c["uri"] != resource.URI || c["mimeType"] != resource.MIME || c["text"] != string(want) {
					t.Fatal("resource bytes or MIME differ")
				}
			}
			for _, p := range []string{`"uri":"https://example.test/remote"`, `"uri":"scotland://docs/nope"`} {
				if r := request("resources/read", p); r.Error == nil || r.Error.Code != -32002 {
					t.Fatal(r)
				}
			}
			for _, p := range []string{`"uri":42`, `"uri":"scotland://docs/method","extra":true`} {
				if r := request("resources/read", p); r.Error == nil || r.Error.Code != codeInvalidParams {
					t.Fatal(r)
				}
			}
		})
	}
	// With the default relative server URL, HTTP serves these exact bytes.
	api, err := httpapi.New(h.svc, h.meter, h.log, "")
	if err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string][]byte{"/v1/method": method, "/v1/openapi.json": spec} {
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != http.StatusOK || rec.Body.String() != string(want) {
			t.Fatal("HTTP and MCP documentation differ", path)
		}
	}
}
