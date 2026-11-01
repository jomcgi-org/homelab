package httpapi

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/prometheus/client_golang/prometheus"
)

func handler(loaded bool) *Handler {
	h, err := New(fixture.Service(loaded), meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)), "")
	if err != nil {
		panic(err)
	}
	return h
}

func TestOpenAPIServerIsThePublicBaseURL(t *testing.T) {
	h, err := New(fixture.Service(true), meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)), "https://jomcgi.dev/scotland")
	if err != nil {
		t.Fatal(err)
	}
	_, body := get(t, h, "/v1/openapi.json")
	servers, _ := body["servers"].([]any)
	if len(servers) != 1 || servers[0].(map[string]any)["url"] != "https://jomcgi.dev/scotland" {
		t.Fatalf("servers = %v", body["servers"])
	}
	if _, ok := body["paths"].(map[string]any)["/v1/plan"]; !ok {
		t.Fatal("rewritten document lost its paths")
	}
}

func get(t *testing.T, h http.Handler, url string, hdr ...string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, url, nil)
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var body map[string]any
	if strings.HasPrefix(rec.Header().Get("Content-Type"), "application/json") && rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("%s: invalid JSON: %v", url, err)
		}
	}
	return rec, body
}

func postCheck(t *testing.T, h http.Handler, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/check", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("check %s: invalid JSON %q: %v", body, rec.Body, err)
	}
	return rec, out
}

func checkMeta(t *testing.T, what string, body map[string]any) {
	t.Helper()
	if _, ok := body["data"]; !ok {
		t.Fatalf("%s: no data", what)
	}
	meta, _ := body["meta"].(map[string]any)
	for _, k := range []string{"api_version", "generated_at", "as_of", "forecast_fetched_at", "data_version", "method_url", "sources"} {
		if _, ok := meta[k]; !ok {
			t.Errorf("%s: meta.%s missing", what, k)
		}
	}
}

const checkBody = `{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"Cairn Gorm"},{"activity":"stargaze","date":"2026-10-19","lat":57.3,"lon":-3.7}]}`

// The envelope contract: every 200 carries data and a complete meta block.
// plan answers are cached at the edge; check answers, being POSTs, are not.
func TestEnvelopeContract(t *testing.T) {
	h := handler(true)
	for _, url := range []string{
		"/v1/plan?lat=57.19&lon=-3.83",
		"/v1/plan?lat=57.19&lon=-3.83&radius_km=100&from=2026-10-20&to=2026-10-26&limit=5&max_duration_h=4.5&max_ascent_m=800",
		"/v1/plan?lat=57.19&lon=-3.83&activities=walk,stargaze",
		"/v1/plan?lat=57.19&lon=-3.83&activities=walk&activities=stargaze",
		"/v1/plan?lat=57.19&lon=-3.83&min_duration_h=1&min_length_km=2&max_length_km=20&max_rain_mm_h=0.5&max_wind_kmh=40&min_temp_c=-2",
	} {
		rec, body := get(t, h, url)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status %d: %s", url, rec.Code, rec.Body)
		}
		if rec.Header().Get("Cache-Control") != CacheControl.browser || rec.Header().Get("ETag") == "" {
			t.Fatalf("%s: cache headers %v", url, rec.Header())
		}
		// Cloudflare reads its own header first; s-maxage there would disable
		// the stale directives, so the edge lifetime must arrive as max-age.
		if edge := rec.Header().Get("Cloudflare-CDN-Cache-Control"); edge != CacheControl.edge || strings.Contains(edge, "s-maxage") {
			t.Fatalf("%s: cache headers %v", url, rec.Header())
		}
		checkMeta(t, url, body)
	}
	rec, body := postCheck(t, h, checkBody)
	if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("Cloudflare-CDN-Cache-Control") != "no-store" || rec.Header().Get("ETag") != "" {
		t.Fatalf("check: status %d headers %v", rec.Code, rec.Header())
	}
	checkMeta(t, "check", body)
	if items := body["data"].(map[string]any)["items"].([]any); len(items) != 2 {
		t.Fatalf("check items %v", items)
	}
}

func TestActivitiesListForms(t *testing.T) {
	h := handler(true)
	for url, want := range map[string]string{
		"/v1/plan?lat=57.19&lon=-3.83&activities=stargaze":                 "stargaze",
		"/v1/plan?lat=57.19&lon=-3.83&activities=stargaze,+walk":           "walk,stargaze",
		"/v1/plan?lat=57.19&lon=-3.83&activities=walk&activities=stargaze": "walk,stargaze",
		"/v1/plan?lat=57.19&lon=-3.83&activities=":                         "walk,stargaze",
	} {
		_, body := get(t, h, url)
		var got []string
		for _, a := range body["data"].(map[string]any)["query"].(map[string]any)["activities"].([]any) {
			got = append(got, a.(string))
		}
		if strings.Join(got, ",") != want {
			t.Errorf("%s: activities %v", url, got)
		}
	}
}

func TestCapsAreEnforced(t *testing.T) {
	h := handler(true)
	for _, url := range []string{
		"/v1/plan",
		"/v1/plan?lat=57.19",
		"/v1/plan?lat=north&lon=-3.8",
		"/v1/plan?lat=51.5&lon=-0.12",
		"/v1/plan?place=Aviemore",
		"/v1/plan?lat=57&lon=-3.8&limit=6",
		"/v1/plan?lat=57&lon=-3.8&limit=ten",
		"/v1/plan?lat=57&lon=-3.8&radius_km=101",
		"/v1/plan?lat=57&lon=-3.8&from=tomorrow",
		"/v1/plan?lat=57&lon=-3.8&from=2026-10-20&to=2026-10-27",
		"/v1/plan?lat=57&lon=-3.8&activities=ski",
		"/v1/plan?lat=57&lon=-3.8&max_ascent_m=lots",
		"/v1/plan?lat=57&lon=-3.8&max_duration_h=0",
		"/v1/plan?lat=57&lon=-3.8&max_rain_mm_h=11",
		"/v1/plan?lat=57&lon=-3.8&max_wind_kmh=windy",
		"/v1/plan?lat=57&lon=-3.8&max_gust_kmh=50",
		"/v1/plan?lat=57&lon=-3.8&max_rain_chance_pct=30",
		"/v1/plan?lat=57&lon=-3.8&min_length_km=10&max_length_km=5",
	} {
		rec, body := get(t, h, url)
		errObj, _ := body["error"].(map[string]any)
		if rec.Code != http.StatusBadRequest || errObj["code"] != "invalid_parameter" || errObj["param"] == "" {
			t.Errorf("%s: status %d body %s", url, rec.Code, rec.Body)
		}
		if rec.Header().Get("Cache-Control") != cacheControlError.browser {
			t.Errorf("%s: cache headers %v", url, rec.Header())
		}
	}
	for _, body := range []string{
		``,
		`{`,
		`[]`,
		`{"items":[]}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm","colour":"red"}]}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm"}],"extra":1}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm"}]} {}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm"}]}]`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm"}]}}`,
		`{"items":[{"activity":"walk","date":"2026-10-20"}]}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"gorm"}],"max_rain_mm_h":11}`,
		`{"items":[{"activity":"stargaze","date":"2026-10-20","lat":"57"}]}`,
		`{"items":[{"activity":"walk","date":"2026-10-20","walk_name":"` + strings.Repeat("a", maxCheckBody) + `"}]}`,
	} {
		rec, out := postCheck(t, h, body)
		errObj, _ := out["error"].(map[string]any)
		if rec.Code != http.StatusBadRequest || errObj["code"] != "invalid_parameter" || errObj["param"] == "" || rec.Header().Get("Cache-Control") != "no-store" {
			t.Errorf("check %.80s: status %d headers %v body %.200s", body, rec.Code, rec.Header(), rec.Body)
		}
	}
}

// A browser POSTing JSON preflights first; the answer allows it from any
// origin, as the GET routes are.
func TestCheckPreflight(t *testing.T) {
	req := httptest.NewRequest(http.MethodOptions, "/v1/check", nil)
	req.Header.Set("Origin", "https://example.org")
	req.Header.Set("Access-Control-Request-Method", "POST")
	req.Header.Set("Access-Control-Request-Headers", "content-type")
	rec := httptest.NewRecorder()
	handler(true).ServeHTTP(rec, req)
	hdr := rec.Header()
	if rec.Code != http.StatusNoContent || hdr.Get("Access-Control-Allow-Origin") != "*" ||
		!strings.Contains(hdr.Get("Access-Control-Allow-Methods"), "POST") || !strings.EqualFold(hdr.Get("Access-Control-Allow-Headers"), "content-type") {
		t.Fatalf("preflight %d %v", rec.Code, hdr)
	}
	// A wrong method on a real endpoint says so, and which methods to use.
	if rec, body := get(t, handler(true), "/v1/check"); rec.Code != http.StatusMethodNotAllowed || rec.Header().Get("Allow") != "POST, OPTIONS" ||
		body["error"].(map[string]any)["code"] != "method_not_allowed" {
		t.Fatalf("GET /v1/check = %d %v", rec.Code, rec.Header())
	}
	post := httptest.NewRecorder()
	handler(true).ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/v1/plan", strings.NewReader("{}")))
	if post.Code != http.StatusMethodNotAllowed || post.Header().Get("Allow") != "GET" {
		t.Fatalf("POST /v1/plan = %d %v", post.Code, post.Header())
	}
}

func TestConditionalGet(t *testing.T) {
	h := handler(true)
	rec, _ := get(t, h, "/v1/plan?lat=57.19&lon=-3.83")
	tag := rec.Header().Get("ETag")
	rec, _ = get(t, h, "/v1/plan?lat=57.19&lon=-3.83", "If-None-Match", `"other", W/`+tag)
	if rec.Code != http.StatusNotModified || rec.Body.Len() != 0 {
		t.Fatalf("conditional GET = %d", rec.Code)
	}
}

func TestUnavailableIsNotCached(t *testing.T) {
	rec, body := get(t, handler(false), "/v1/plan?lat=57.19&lon=-3.83")
	if rec.Code != http.StatusServiceUnavailable || rec.Header().Get("Cache-Control") != "no-store" ||
		rec.Header().Get("Cloudflare-CDN-Cache-Control") != "no-store" || rec.Header().Get("Retry-After") == "" || body["error"] == nil {
		t.Fatalf("status %d headers %v", rec.Code, rec.Header())
	}
	if rec, _ := postCheck(t, handler(false), checkBody); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("check status %d", rec.Code)
	}
}

func TestUnknownPathIs404(t *testing.T) {
	for _, p := range []string{"/v1/sites", "/v1/dark-sky-windows", "/v1/walk-windows"} {
		rec, body := get(t, handler(true), p)
		if rec.Code != http.StatusNotFound || body["error"] == nil {
			t.Fatalf("%s: status %d", p, rec.Code)
		}
	}
}

type specSchema struct {
	Type       any                   `json:"type"`
	Ref        string                `json:"$ref"`
	Minimum    *float64              `json:"minimum"`
	Maximum    *float64              `json:"maximum"`
	Default    any                   `json:"default"`
	MaxLength  *int                  `json:"maxLength"`
	MinItems   *int                  `json:"minItems"`
	MaxItems   *int                  `json:"maxItems"`
	Enum       []string              `json:"enum"`
	Required   []string              `json:"required"`
	Properties map[string]specSchema `json:"properties"`
	Items      *specSchema           `json:"items"`
}

// openapi.json is the published contract: its operations must be exactly
// the served routes and its caps must equal the code's.
func TestOpenAPIMatchesCode(t *testing.T) {
	var spec struct {
		Paths map[string]map[string]struct {
			OperationID string `json:"operationId"`
			Parameters  []struct {
				Name        string     `json:"name"`
				Required    bool       `json:"required"`
				Description string     `json:"description"`
				Schema      specSchema `json:"schema"`
			} `json:"parameters"`
		} `json:"paths"`
		Components struct {
			Schemas map[string]specSchema `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(openAPI, &spec); err != nil {
		t.Fatal(err)
	}
	h := handler(true)
	var ops []string
	for path, methods := range spec.Paths {
		for method := range methods {
			ops = append(ops, strings.ToUpper(method)+" "+path)
			query := "?lat=57.19&lon=-3.83"
			if path == "/v1/aurora" {
				query += "&at=2026-10-20T23:00:00Z"
			}
			req := httptest.NewRequest(strings.ToUpper(method), path+query, strings.NewReader(checkBody))
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != http.StatusOK {
				t.Errorf("spec operation %s %s = %d", method, path, rec.Code)
			}
		}
	}
	sort.Strings(ops)
	if got := strings.Join(ops, ","); got != "GET /v1/aurora,GET /v1/light,GET /v1/method,GET /v1/openapi.json,GET /v1/plan,POST /v1/check" {
		t.Errorf("spec operations = %s", got)
	}

	bounds := func(where string, s specSchema, lo, hi float64) {
		t.Helper()
		if s.Minimum == nil || s.Maximum == nil || *s.Minimum != lo || *s.Maximum != hi {
			t.Errorf("%s = %+v, code %v..%v", where, s, lo, hi)
		}
	}
	defaults := func(where string, s specSchema, want float64) {
		t.Helper()
		if d, ok := s.Default.(float64); !ok || d != want {
			t.Errorf("%s default = %v, code %v", where, s.Default, want)
		}
	}
	c := connector.Coverage
	activities := connector.ActivityWalk + "," + connector.ActivityStargaze
	seen := map[string]bool{}
	for _, p := range spec.Paths["/v1/plan"]["get"].Parameters {
		seen[p.Name] = true
		where := "plan." + p.Name
		switch p.Name {
		case "lat":
			bounds(where, p.Schema, c.MinLat, c.MaxLat)
		case "lon":
			bounds(where, p.Schema, c.MinLon, c.MaxLon)
		case "radius_km":
			bounds(where, p.Schema, 1, connector.MaxRadiusKm)
			defaults(where, p.Schema, connector.DefaultRadiusKm)
		case "limit":
			bounds(where, p.Schema, 1, connector.MaxOptions)
			defaults(where, p.Schema, connector.DefaultOptions)
		case "min_duration_h", "max_duration_h":
			bounds(where, p.Schema, 0.5, connector.MaxDurationH)
		case "min_length_km", "max_length_km":
			bounds(where, p.Schema, 0.5, connector.MaxLengthKm)
		case "max_rain_mm_h":
			bounds(where, p.Schema, 0, connector.MaxRainLimitMM)
		case "max_wind_kmh":
			bounds(where, p.Schema, 0, connector.MaxWindLimitKmh)
		case "min_temp_c":
			bounds(where, p.Schema, connector.MinTempLimitC, connector.MaxTempLimitC)
		case "max_ascent_m":
			bounds(where, p.Schema, 1, connector.MaxAscentM)
		case "from":
			if !strings.Contains(p.Description, fmt.Sprintf("up to %d days ahead", connector.MaxDaysAhead)) {
				t.Errorf("%s description %q does not match the code's window", where, p.Description)
			}
		case "to":
			if !strings.Contains(p.Description, fmt.Sprintf("at most %d days", connector.MaxSpanDays)) {
				t.Errorf("%s description %q does not match the code's span", where, p.Description)
			}
		case "activities":
			if p.Schema.Items == nil || strings.Join(p.Schema.Items.Enum, ",") != activities {
				t.Errorf("%s = %+v", where, p.Schema)
			}
		default:
			t.Errorf("spec has unknown plan parameter %s", p.Name)
		}
		if p.Required != (p.Name == "lat" || p.Name == "lon") {
			t.Errorf("%s required = %v", where, p.Required)
		}
	}
	if len(seen) != 15 {
		t.Errorf("plan parameters = %v", seen)
	}

	schemas := spec.Components.Schemas
	req := schemas["CheckRequest"]
	bounds("CheckRequest.max_rain_mm_h", req.Properties["max_rain_mm_h"], 0, connector.MaxRainLimitMM)
	bounds("CheckRequest.max_wind_kmh", req.Properties["max_wind_kmh"], 0, connector.MaxWindLimitKmh)
	bounds("CheckRequest.min_temp_c", req.Properties["min_temp_c"], connector.MinTempLimitC, connector.MaxTempLimitC)
	items := req.Properties["items"]
	if items.MinItems == nil || *items.MinItems != 1 || items.MaxItems == nil || *items.MaxItems != connector.MaxCheckItems {
		t.Errorf("CheckRequest.items = %+v", items)
	}
	item := schemas["CheckItem"]
	bounds("CheckItem.lat", item.Properties["lat"], c.MinLat, c.MaxLat)
	bounds("CheckItem.lon", item.Properties["lon"], c.MinLon, c.MaxLon)
	if p := item.Properties["walk_name"]; p.MaxLength == nil || *p.MaxLength != connector.MaxNameLength {
		t.Errorf("CheckItem.walk_name = %+v", p)
	}
	if strings.Join(item.Properties["activity"].Enum, ",") != activities || strings.Join(item.Required, ",") != "activity,date" {
		t.Errorf("CheckItem = %+v", item)
	}
	var want []string
	for _, v := range verdict.All {
		want = append(want, string(v))
	}
	if got := strings.Join(schemas["Verdict"].Enum, ","); got != strings.Join(want, ",") {
		t.Errorf("Verdict = %s, code %v", got, want)
	}
}

func TestLightHTTPRoundTripAndCache(t *testing.T) {
	h := handler(true)
	url := "/v1/light?lat=57.3&lon=-3.7&from=2026-10-19&to=2026-10-21"
	rec, body := get(t, h, url)
	if rec.Code != 200 {
		t.Fatal(rec.Code, rec.Body)
	}
	checkMeta(t, "light", body)
	env, err := fixture.Service(true).Light(fixture.LightParams())
	if err != nil {
		t.Fatal(err)
	}
	want, _ := json.Marshal(env)
	got, _ := json.Marshal(body)
	var wantBody map[string]any
	if err := json.Unmarshal(want, &wantBody); err != nil {
		t.Fatal(err)
	}
	want, _ = json.Marshal(wantBody)
	if string(want) != string(got) {
		t.Fatal("REST differs from shared service")
	}
	for _, header := range []string{"Cache-Control", "Cloudflare-CDN-Cache-Control"} {
		plan, _ := get(t, h, "/v1/plan?lat=57.3&lon=-3.7")
		if rec.Header().Get(header) != plan.Header().Get(header) {
			t.Fatal(header, rec.Header())
		}
	}
	if rec.Header().Get("Set-Cookie") != "" || rec.Header().Get("ETag") == "" {
		t.Fatal(rec.Header())
	}
	notModified, _ := get(t, h, url, "If-None-Match", rec.Header().Get("ETag"))
	if notModified.Code != 304 || notModified.Body.Len() != 0 || notModified.Header().Get("Cloudflare-CDN-Cache-Control") != CacheControl.edge {
		t.Fatal(notModified)
	}
	other, _ := get(t, h, strings.Replace(url, "lon=-3.7", "lon=-4.7", 1), "If-None-Match", rec.Header().Get("ETag"))
	if other.Code != 200 || other.Header().Get("ETag") == rec.Header().Get("ETag") {
		t.Fatal("query cache isolation")
	}
	missing, missingBody := get(t, handler(false), url)
	if missing.Code != 200 || missingBody["data"].(map[string]any)["site"] != nil {
		t.Fatal("astronomy unavailable without snapshot")
	}
	for _, suffix := range []string{"", "?lat=NaN&lon=-3.7", "?lat=57&lon=Inf", "?lat=57&lon=-4&radius_km=101", "?lat=57&lon=-4&lat=58", "?lat=57&lon=-4&from=no", "?lat=57&lon=-4&to=2026-10-26", "?lat=57&lon=-4&extra=1"} {
		r, b := get(t, h, "/v1/light"+suffix)
		if r.Code != 400 || b["error"] == nil || r.Header().Get("Cache-Control") != cacheControlError.browser || r.Header().Get("Cloudflare-CDN-Cache-Control") != cacheControlError.edge {
			t.Fatal(suffix, r.Code, r.Header())
		}
	}
	for _, method := range []string{"POST", "DELETE", "OPTIONS"} {
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest(method, "/v1/light", nil))
		if r.Code != 405 || r.Header().Get("Allow") != "GET" {
			t.Fatal(method, r.Code, r.Header())
		}
	}
}

func TestLightHTTPKindsIdentityAndErrors(t *testing.T) {
	h := handler(true)
	base := "/v1/light?lat=57.3&lon=-3.7"
	a, _ := get(t, h, base+"&kinds=blue,golden,blue")
	b, body := get(t, h, base+"&kinds=%20golden%20,blue")
	if a.Code != 200 || b.Code != 200 || a.Header().Get("ETag") != b.Header().Get("ETag") || a.Body.String() != b.Body.String() {
		t.Fatal("noncanonical filter", a.Body, b.Body)
	}
	for _, raw := range body["data"].(map[string]any)["days"].([]any) {
		for _, raw := range raw.(map[string]any)["windows"].([]any) {
			kind := raw.(map[string]any)["kind"]
			if kind != "golden" && kind != "blue" {
				t.Fatal("filter leaked", kind)
			}
		}
	}
	all, _ := get(t, h, base)
	explicit, _ := get(t, h, base+"&kinds=milky_way,darkness,astronomical,nautical,civil,blue,golden")
	if all.Code != 200 || explicit.Code != 200 || all.Header().Get("ETag") != explicit.Header().Get("ETag") {
		t.Fatal("all filter identity")
	}
	other, _ := get(t, h, base+"&kinds=golden", "If-None-Match", a.Header().Get("ETag"))
	if other.Code != 200 || other.Header().Get("ETag") == a.Header().Get("ETag") {
		t.Fatal("distinct filters collide")
	}
	cached, _ := get(t, h, base+"&kinds=golden,blue", "If-None-Match", a.Header().Get("ETag"))
	if cached.Code != 304 {
		t.Fatal("canonical filter not revalidated", cached.Code)
	}
	for _, q := range []string{"&kinds=", "&kinds=golden,", "&kinds=golden,,blue", "&kinds=cloud", "&kinds=blue&kinds=golden", "&kinds=GOLDEN"} {
		bad, _ := get(t, h, base+q)
		if bad.Code != 400 {
			t.Fatalf("%s: %d", q, bad.Code)
		}
	}
}

// Check the fixture's complete response against the published response shape,
// including nullable events, required metadata, enums and nested references.
func TestLightOpenAPIResponse(t *testing.T) {
	var doc map[string]any
	if err := json.Unmarshal(openAPI, &doc); err != nil {
		t.Fatal(err)
	}
	schemas := doc["components"].(map[string]any)["schemas"].(map[string]any)
	var validate func(any, map[string]any, string)
	validate = func(value any, s map[string]any, path string) {
		if ref, ok := s["$ref"].(string); ok {
			name := strings.TrimPrefix(ref, "#/components/schemas/")
			target, ok := schemas[name].(map[string]any)
			if !ok {
				t.Fatalf("missing ref %s", ref)
			}
			validate(value, target, path)
			return
		}
		if value == nil {
			raw, _ := json.Marshal(s["type"])
			if !strings.Contains(string(raw), "null") {
				t.Errorf("%s unexpected null", path)
			}
			return
		}
		if e, ok := s["enum"].([]any); ok {
			found := false
			for _, v := range e {
				if v == value {
					found = true
				}
			}
			if !found {
				t.Errorf("%s invalid enum %v", path, value)
			}
		}
		switch v := value.(type) {
		case map[string]any:
			if req, ok := s["required"].([]any); ok {
				for _, key := range req {
					if _, ok := v[key.(string)]; !ok {
						t.Errorf("%s missing %v", path, key)
					}
				}
			}
			props, _ := s["properties"].(map[string]any)
			for key, item := range v {
				child, ok := props[key].(map[string]any)
				if !ok {
					t.Errorf("%s undocumented field %s", path, key)
					continue
				}
				validate(item, child, path+"."+key)
			}
		case []any:
			item, ok := s["items"].(map[string]any)
			if !ok {
				t.Fatalf("%s undocumented array", path)
			}
			for _, x := range v {
				validate(x, item, path+"[]")
			}
		case float64:
			if lo, ok := s["minimum"].(float64); ok && v < lo {
				t.Errorf("%s below minimum", path)
			}
			if hi, ok := s["maximum"].(float64); ok && v > hi {
				t.Errorf("%s above maximum", path)
			}
		}
	}
	_, body := get(t, handler(true), "/v1/light?lat=57.3&lon=-3.7")
	validate(body, schemas["LightResponse"].(map[string]any), "response")
	op := doc["paths"].(map[string]any)["/v1/light"].(map[string]any)["get"].(map[string]any)
	params := op["parameters"].([]any)
	if len(params) != 6 {
		t.Fatal(params)
	}
	for _, raw := range params {
		p := raw.(map[string]any)
		s := p["schema"].(map[string]any)
		switch p["name"] {
		case "lat":
			if s["minimum"] != connector.Coverage.MinLat || s["maximum"] != connector.Coverage.MaxLat || p["required"] != true {
				t.Fatal(p)
			}
		case "lon":
			if s["minimum"] != connector.Coverage.MinLon || s["maximum"] != connector.Coverage.MaxLon || p["required"] != true {
				t.Fatal(p)
			}
		case "radius_km":
			if s["maximum"] != float64(connector.MaxRadiusKm) || s["default"] != float64(connector.DefaultRadiusKm) {
				t.Fatal(p)
			}
		}
	}
}
