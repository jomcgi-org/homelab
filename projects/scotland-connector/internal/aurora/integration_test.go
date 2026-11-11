package aurora_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/store"
	"github.com/prometheus/client_golang/prometheus"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type objectGetter struct {
	body        []byte
	tag         string
	fail        bool
	notModified bool
}

func (g *objectGetter) Get(_ context.Context, bucket, key, tag string, limit int64) (r2.Object, error) {
	if bucket != "stars" || key != "aurora.json" || limit != aurora.MaxBytes {
		return r2.Object{}, errors.New("wrong storage contract")
	}
	if g.fail {
		return r2.Object{}, errors.New("fixture read failed")
	}
	return r2.Object{Body: g.body, ETag: g.tag, NotModified: g.notModified}, nil
}

func TestEnabledProducerStoreTransportParityAndExpiry(t *testing.T) {
	in, err := fixture.AuroraInputs()
	if err != nil {
		t.Fatal(err)
	}
	requests := 0
	client := &http.Client{Transport: roundTrip(func(r *http.Request) (*http.Response, error) {
		requests++
		if r.Header.Get("User-Agent") == "" || r.Header.Get("Authorization") != "" {
			t.Fatal("collection headers")
		}
		body := fixture.AuroraNOAA
		switch r.URL.Host {
		case "services.swpc.noaa.gov":
			if r.URL.String() != aurora.NOAAURL {
				t.Fatal(r.URL)
			}
		case "api.met.no":
			if r.URL.Query().Get("lat") != "58" || r.URL.Query().Get("lon") != "-4" {
				t.Fatal(r.URL)
			}
			body = fixture.AuroraMET
		default:
			t.Fatal("unexpected upstream")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{}}, nil
	})}
	in, err = aurora.Collect(context.Background(), client, in.Weather, func() time.Time { return fixture.AuroraNow })
	if err != nil {
		t.Fatal(err)
	}
	b, err := aurora.Materialize(in)
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	g := &objectGetter{body: b, tag: "fixture1"}
	st := store.NewAurora("stars", "aurora.json", time.Minute, g, log)
	if err := st.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	first := st.Current()
	// A chronologically older NOAA observation cannot replace newer evidence.
	older := *first
	older.NOAA = first.NOAA
	older.NOAA.ObservedAt = older.NOAA.ObservedAt.Add(-time.Minute)
	g.body, _ = json.Marshal(older)
	g.tag = "regressive"
	if st.Refresh(context.Background()) == nil || st.Current() != first {
		t.Fatal("regressive observation replaced current")
	}
	g.body = b
	g.tag = "fixture1"
	now := fixture.AuroraNow
	svc := &connector.Service{AuroraData: st, MethodURL: "/v1/method", Now: func() time.Time { return now }}
	reg := prometheus.NewRegistry()
	metrics := meter.New(reg)
	api, err := httpapi.New(svc, metrics, log, "")
	if err != nil {
		t.Fatal(err)
	}
	m := mcp.New(svc, metrics, log, mcp.ServerInfo{Name: "fixture"}, nil)
	assert := func(want string) {
		t.Helper()
		q := url.Values{"lat": {"58"}, "lon": {"-4"}, "at": {"2026-12-01T23:00:00Z"}}
		r := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/v1/aurora?"+q.Encode(), nil)
		req.Header.Set("If-None-Match", "*")
		api.ServeHTTP(r, req)
		if r.Code != 200 || r.Header().Get("Cache-Control") != "no-store" || r.Header().Get("Cloudflare-CDN-Cache-Control") != "no-store" {
			t.Fatalf("REST: %d %s %v", r.Code, r.Body, r.Header())
		}
		var rest map[string]any
		if err := json.Unmarshal(r.Body.Bytes(), &rest); err != nil {
			t.Fatal(err)
		}
		body := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"aurora","arguments":{"lat":58,"lon":-4,"at":"2026-12-01T23:00:00Z"}}}`
		r = httptest.NewRecorder()
		m.ServeHTTP(r, httptest.NewRequest("POST", "/mcp", strings.NewReader(body)))
		var rpc struct {
			Result struct {
				StructuredContent map[string]any `json:"structuredContent"`
				IsError           bool           `json:"isError"`
			}
		}
		if err := json.Unmarshal(r.Body.Bytes(), &rpc); err != nil {
			t.Fatal(err)
		}
		x, _ := json.Marshal(rest)
		y, _ := json.Marshal(rpc.Result.StructuredContent)
		if rpc.Result.IsError || string(x) != string(y) {
			t.Fatalf("parity: %s != %s (%s)", x, y, r.Body)
		}
		d := rest["data"].(map[string]any)
		if d["verdict"] != want {
			t.Fatalf("want %s: %s", want, x)
		}
		if len(rest["meta"].(map[string]any)["sources"].([]any)) != 3 || d["policy"] != aurora.Policy {
			t.Fatal("missing provenance")
		}
	}
	assert("go")
	// Both parse failure and fetch failure retain the same source timestamps.
	g.body = []byte(`{"schema":1}`)
	g.tag = "broken"
	if st.Refresh(context.Background()) == nil || st.Current() != first {
		t.Fatal("bad replacement")
	}
	g.fail = true
	if st.Refresh(context.Background()) == nil || st.Current() != first {
		t.Fatal("failed replacement")
	}
	now = now.Add(aurora.NOAAMaxAge)
	assert("unknown")
	g.fail = false
	g.notModified = true
	if err := st.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	assert("unknown")
	if requests != 2 {
		t.Fatalf("request path collected upstream: %d", requests)
	}
	for _, surface := range []string{"api", "mcp"} {
		families, err := reg.Gather()
		if err != nil {
			t.Fatal(err)
		}
		found := false
		for _, f := range families {
			if f.GetName() != "scotland_connector_calls_total" {
				continue
			}
			for _, metric := range f.Metric {
				labels := map[string]string{}
				for _, l := range metric.Label {
					labels[l.GetName()] = l.GetValue()
				}
				if labels["tool"] == "aurora" && labels["surface"] == surface && metric.Counter.GetValue() == 3 {
					found = true
				}
			}
		}
		if !found {
			t.Fatalf("no aurora %s accounting", surface)
		}
	}
}

func TestDefaultOffAndParameterParity(t *testing.T) {
	svc := &connector.Service{Now: func() time.Time { return fixture.AuroraNow }}
	lat, lon := 58.0, -4.0
	e, err := svc.Aurora(connector.AuroraParams{Lat: &lat, Lon: &lon, At: "2026-12-01T23:00:00Z"})
	if err != nil || e.Data.(aurora.Assessment).Verdict != "unknown" {
		t.Fatal(e, err)
	}
	for _, p := range []connector.AuroraParams{{At: "2026-12-01T23:00:00Z"}, {Lat: ptr(90), Lon: &lon, At: "2026-12-01T23:00:00Z"}, {Lat: &lat, Lon: &lon, At: ""}, {Lat: &lat, Lon: &lon, At: "2026-12-01T23:00:00"}, {Lat: ptr(math.Inf(1)), Lon: &lon, At: "2026-12-01T23:00:00Z"}, {Lat: ptr(math.NaN()), Lon: &lon, At: "2026-12-01T23:00:00Z"}} {
		if _, err := svc.Aurora(p); err == nil {
			t.Fatal("invalid params accepted", p)
		}
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	reg := prometheus.NewRegistry()
	metrics := meter.New(reg)
	api, _ := httpapi.New(svc, metrics, log, "")
	for _, q := range []string{"lat=58&lon=-4&at=x", "lat=NaN&lon=-4&at=2026-12-01T23:00:00Z", "lat=58&lat=59&lon=-4", "lat=58&lon=-4&extra=x", strings.Repeat("x", 1025)} {
		r := httptest.NewRecorder()
		api.ServeHTTP(r, httptest.NewRequest("GET", "/v1/aurora?"+q, nil))
		if r.Code != 400 {
			t.Fatal(q, r.Code)
		}
	}
}
