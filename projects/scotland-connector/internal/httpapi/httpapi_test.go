package httpapi

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

func handler(loaded bool) *Handler {
	return New(fixture.Service(loaded), meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)))
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

// The envelope contract: every 200 carries data and a complete meta block.
func TestEnvelopeContract(t *testing.T) {
	h := handler(true)
	for _, url := range []string{
		"/v1/dark-sky-windows?place=Aviemore",
		"/v1/dark-sky-windows?lat=57.19&lon=-3.83&radius_km=80&nights=7&limit=10",
		"/v1/places/resolve?q=Aviemore",
	} {
		rec, body := get(t, h, url)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status %d: %s", url, rec.Code, rec.Body)
		}
		if rec.Header().Get("Cache-Control") != CacheControl || rec.Header().Get("ETag") == "" {
			t.Fatalf("%s: cache headers %v", url, rec.Header())
		}
		if _, ok := body["data"]; !ok {
			t.Fatalf("%s: no data", url)
		}
		meta, _ := body["meta"].(map[string]any)
		for _, k := range []string{"api_version", "generated_at", "as_of", "forecast_fetched_at", "data_version", "method_url", "sources"} {
			if _, ok := meta[k]; !ok {
				t.Errorf("%s: meta.%s missing", url, k)
			}
		}
	}
}

func TestCapsAreEnforced(t *testing.T) {
	h := handler(true)
	for _, url := range []string{
		"/v1/dark-sky-windows",
		"/v1/dark-sky-windows?lat=57&lon=-3.8&limit=11",
		"/v1/dark-sky-windows?lat=57&lon=-3.8&radius_km=151",
		"/v1/dark-sky-windows?lat=57&lon=-3.8&nights=8",
		"/v1/dark-sky-windows?lat=57&lon=-3.8&limit=ten",
		"/v1/dark-sky-windows?lat=north&lon=-3.8",
		"/v1/dark-sky-windows?lat=51.5&lon=-0.12",
		"/v1/places/resolve",
		"/v1/places/resolve?q=" + strings.Repeat("a", connector.MaxQueryLength+1),
	} {
		rec, body := get(t, h, url)
		errObj, _ := body["error"].(map[string]any)
		if rec.Code != http.StatusBadRequest || errObj["code"] != "invalid_parameter" || errObj["param"] == "" {
			t.Errorf("%s: status %d body %s", url, rec.Code, rec.Body)
		}
	}
}

func TestConditionalGet(t *testing.T) {
	h := handler(true)
	rec, _ := get(t, h, "/v1/dark-sky-windows?place=Aviemore")
	tag := rec.Header().Get("ETag")
	rec, _ = get(t, h, "/v1/dark-sky-windows?place=Aviemore", "If-None-Match", `"other", W/`+tag)
	if rec.Code != http.StatusNotModified || rec.Body.Len() != 0 {
		t.Fatalf("conditional GET = %d", rec.Code)
	}
}

func TestUnavailableIsNotCached(t *testing.T) {
	rec, body := get(t, handler(false), "/v1/dark-sky-windows?place=Aviemore")
	if rec.Code != http.StatusServiceUnavailable || rec.Header().Get("Cache-Control") != "no-store" || body["error"] == nil {
		t.Fatalf("status %d headers %v", rec.Code, rec.Header())
	}
}

func TestUnknownPathIs404(t *testing.T) {
	rec, body := get(t, handler(true), "/v1/sites")
	if rec.Code != http.StatusNotFound || body["error"] == nil {
		t.Fatalf("status %d", rec.Code)
	}
}

// openapi.json is the published contract: its paths must be exactly the
// served routes and its caps must equal the code's.
func TestOpenAPIMatchesCode(t *testing.T) {
	var spec struct {
		Paths map[string]map[string]struct {
			OperationID string `json:"operationId"`
			Parameters  []struct {
				Name   string `json:"name"`
				Schema struct {
					Minimum   *float64 `json:"minimum"`
					Maximum   *float64 `json:"maximum"`
					Default   *float64 `json:"default"`
					MaxLength *int     `json:"maxLength"`
				} `json:"schema"`
			} `json:"parameters"`
		} `json:"paths"`
	}
	if err := json.Unmarshal(openAPI, &spec); err != nil {
		t.Fatal(err)
	}
	h := handler(true)
	for path := range spec.Paths {
		rec, _ := get(t, h, path+"?place=Aviemore&q=Aviemore")
		if rec.Code == http.StatusNotFound {
			t.Errorf("spec path %s is not served", path)
		}
	}
	want := map[string][3]float64{ // min, max, default
		"radius_km": {1, connector.MaxRadiusKm, connector.DefaultRadiusKm},
		"nights":    {1, connector.MaxNights, connector.DefaultNights},
		"limit":     {1, connector.MaxLimit, connector.DefaultLimit},
	}
	for _, p := range spec.Paths["/v1/dark-sky-windows"]["get"].Parameters {
		if w, ok := want[p.Name]; ok {
			s := p.Schema
			if s.Minimum == nil || s.Maximum == nil || s.Default == nil || *s.Minimum != w[0] || *s.Maximum != w[1] || *s.Default != w[2] {
				t.Errorf("spec %s = %+v, code %v", p.Name, s, w)
			}
			delete(want, p.Name)
		}
		if p.Name == "place" && (p.Schema.MaxLength == nil || *p.Schema.MaxLength != connector.MaxQueryLength) {
			t.Errorf("spec place maxLength = %v", p.Schema.MaxLength)
		}
	}
	if len(want) != 0 {
		t.Errorf("spec lacks parameters %v", want)
	}
}
