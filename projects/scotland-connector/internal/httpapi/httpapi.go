// Package httpapi serves the connector's REST API under /v1.
package httpapi

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
)

//go:embed openapi.json
var openAPI []byte

//go:embed method.md
var method []byte

// Cache policies, each a pair: Cache-Control for browsers and other shared
// caches, and Cloudflare-CDN-Cache-Control for the edge. Cloudflare treats
// s-maxage as proxy-revalidate, which disables the stale directives, so the
// edge lifetime goes to Cloudflare as max-age in its own higher-precedence
// header (the public-tier cache contract, docs/runbooks/public-tier-checklist.md).
type cachePolicy struct{ browser, edge string }

var (
	// CacheControl for answers: forecasts move hourly, so the edge holds an
	// answer for ten minutes, may serve it stale for an hour while
	// revalidating, and keeps answering for a day if the origin is down.
	// Browsers revalidate after a minute.
	CacheControl = cachePolicy{
		browser: "public, max-age=60, s-maxage=600, stale-while-revalidate=3600, stale-if-error=86400",
		edge:    "public, max-age=600, stale-while-revalidate=3600, stale-if-error=86400",
	}
	// cacheControlStatic is for the spec and the method page, which change
	// only with a deploy.
	cacheControlStatic = cachePolicy{
		browser: "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400, stale-if-error=604800",
		edge:    "public, max-age=3600, stale-while-revalidate=86400, stale-if-error=604800",
	}
	// cacheControlError lets the edge absorb repeated bad requests briefly.
	cacheControlError = cachePolicy{
		browser: "public, max-age=60, s-maxage=60",
		edge:    "public, max-age=60",
	}
	// noStore is for answers that must not be cached anywhere.
	noStore = cachePolicy{browser: "no-store", edge: "no-store"}
)

func (p cachePolicy) set(h http.Header) {
	h.Set("Cache-Control", p.browser)
	h.Set("Cloudflare-CDN-Cache-Control", p.edge)
}

const surface = "api"

// Handler serves /v1.
type Handler struct {
	svc   *connector.Service
	meter *meter.Meter
	log   *slog.Logger
	mux   *http.ServeMux
	// specURL is where the published OpenAPI document lives, for 404 hints.
	specURL string
}

// New wires the routes. publicBaseURL is where clients reach the API (for
// example https://jomcgi.dev/scotland behind a prefix-stripping route); the
// published OpenAPI document names it as its server so generated clients
// call the right paths. Empty keeps the document's relative "/".
func New(svc *connector.Service, m *meter.Meter, log *slog.Logger, publicBaseURL string) (*Handler, error) {
	spec, err := specWithServer(openAPI, publicBaseURL)
	if err != nil {
		return nil, err
	}
	h := &Handler{svc: svc, meter: m, log: log, mux: http.NewServeMux(), specURL: publicBaseURL + "/v1/openapi.json"}
	h.mux.HandleFunc("GET /v1/plan", h.plan)
	h.mux.HandleFunc("POST /v1/check", h.check)
	h.mux.HandleFunc("GET /v1/aurora", h.aurora)
	h.mux.HandleFunc("/v1/aurora", h.methodNotAllowed("GET"))
	h.mux.HandleFunc("OPTIONS /v1/check", h.preflight)
	// Without these the /v1/ catch-all would answer a wrong method with "no
	// such endpoint".
	h.mux.HandleFunc("/v1/plan", h.methodNotAllowed("GET"))
	h.mux.HandleFunc("/v1/check", h.methodNotAllowed("POST, OPTIONS"))
	h.mux.HandleFunc("GET /v1/openapi.json", h.static(spec, "application/json"))
	h.mux.HandleFunc("GET /v1/method", h.static(method, "text/markdown; charset=utf-8"))
	h.mux.HandleFunc("/v1/", h.notFound)
	return h, nil
}

// specWithServer rewrites the document's servers list to the public base URL.
func specWithServer(spec []byte, baseURL string) ([]byte, error) {
	if baseURL == "" {
		return spec, nil
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(spec, &doc); err != nil {
		return nil, fmt.Errorf("httpapi: parse openapi.json: %w", err)
	}
	servers, err := json.Marshal([]map[string]string{{"url": baseURL}})
	if err != nil {
		return nil, err
	}
	doc["servers"] = servers
	return json.Marshal(doc)
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	// ServeMux would 301 an unclean path to a root-relative Location, which
	// escapes a prefix-stripping route; answer those with the JSON 404.
	if p := r.URL.Path; p == "/v1" || p != path.Clean(p) {
		h.notFound(w, r)
		return
	}
	h.mux.ServeHTTP(w, r)
}

func (h *Handler) plan(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	q := r.URL.Query()
	var p connector.PlanParams
	var err error
	p.Lat, err = floatParam(q.Get("lat"), "lat", err)
	p.Lon, err = floatParam(q.Get("lon"), "lon", err)
	p.RadiusKm, err = floatParam(q.Get("radius_km"), "radius_km", err)
	p.MinDurationH, err = floatParam(q.Get("min_duration_h"), "min_duration_h", err)
	p.MaxDurationH, err = floatParam(q.Get("max_duration_h"), "max_duration_h", err)
	p.MinLengthKm, err = floatParam(q.Get("min_length_km"), "min_length_km", err)
	p.MaxLengthKm, err = floatParam(q.Get("max_length_km"), "max_length_km", err)
	p.MaxAscentM, err = intParam(q.Get("max_ascent_m"), "max_ascent_m", err)
	p.MaxRainMMH, err = floatParam(q.Get("max_rain_mm_h"), "max_rain_mm_h", err)
	p.MaxWindKmh, err = floatParam(q.Get("max_wind_kmh"), "max_wind_kmh", err)
	p.MinTempC, err = floatParam(q.Get("min_temp_c"), "min_temp_c", err)
	p.Limit, err = intParam(q.Get("limit"), "limit", err)
	p.From, p.To = strParam(q, "from"), strParam(q, "to")
	// activities is comma-separated, repeated, or both.
	for _, v := range q["activities"] {
		for _, a := range strings.Split(v, ",") {
			if a = strings.TrimSpace(a); a != "" {
				p.Activities = append(p.Activities, a)
			}
		}
	}
	// Plan ignores unknown query keys, so the withdrawn limits are refused by
	// name: a caller sending one would otherwise get an answer it never applied.
	for _, name := range []string{"max_gust_kmh", "max_rain_chance_pct"} {
		if err == nil && q.Has(name) {
			err = &connector.ParamError{Param: name, Message: "not offered: MET Norway forecasts gusts and the chance of rain only within its Nordic model, which in Scotland covers Shetland alone"}
		}
	}
	var env *connector.Envelope
	if err == nil {
		env, err = h.svc.Plan(p)
	}
	h.respond(w, r, "plan", start, env, err)
}

// maxCheckBody bounds a check request: ten items are well under 4 KiB.
const maxCheckBody = 16 << 10

func (h *Handler) check(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	var p connector.CheckParams
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxCheckBody))
	dec.DisallowUnknownFields()
	var err error
	// One object and nothing after it: a second Decode must hit EOF.
	if derr := dec.Decode(&p); derr != nil || dec.Decode(&json.RawMessage{}) != io.EOF {
		err = &connector.ParamError{Param: "body", Message: `a JSON object {"items": [...]} with known fields only, at most 16 KiB`}
	}
	var env *connector.Envelope
	if err == nil {
		env, err = h.svc.Check(p)
	}
	h.respond(w, r, "check", start, env, err)
}

// preflight answers the CORS preflight a browser sends before a JSON POST.
func (h *Handler) preflight(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
	w.Header().Set("Access-Control-Max-Age", "86400")
	cacheControlStatic.set(w.Header())
	w.WriteHeader(http.StatusNoContent)
}

// strParam is nil for an absent parameter, so the service applies its default.
func strParam(q url.Values, name string) *string {
	if !q.Has(name) {
		return nil
	}
	v := q.Get(name)
	return &v
}

func floatParam(raw, name string, prev error) (*float64, error) {
	if prev != nil || raw == "" {
		return nil, prev
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return nil, &connector.ParamError{Param: name, Message: "must be a number"}
	}
	return &v, nil
}

func intParam(raw, name string, prev error) (*int, error) {
	if prev != nil || raw == "" {
		return nil, prev
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return nil, &connector.ParamError{Param: name, Message: "must be an integer"}
	}
	return &v, nil
}

type errorBody struct {
	Error struct {
		Code    string `json:"code"`
		Param   string `json:"param,omitempty"`
		Message string `json:"message"`
	} `json:"error"`
	Meta connector.Meta `json:"meta"`
}

func (h *Handler) respond(w http.ResponseWriter, r *http.Request, tool string, start time.Time, env *connector.Envelope, err error) {
	class := meter.ClientClass(r)
	if err != nil {
		var body errorBody
		body.Meta = connector.Meta{APIVersion: connector.APIVersion, GeneratedAt: time.Now().UTC().Format(time.RFC3339), MethodURL: h.svc.MethodURL, Sources: []connector.Source{}}
		status, outcome := http.StatusBadRequest, "invalid"
		var pe *connector.ParamError
		switch {
		case errors.As(err, &pe):
			body.Error.Code, body.Error.Param, body.Error.Message = "invalid_parameter", pe.Param, pe.Message
			if r.Method == http.MethodPost || tool == "aurora" {
				noStore.set(w.Header())
			} else {
				cacheControlError.set(w.Header())
			}
		case errors.Is(err, connector.ErrUnavailable):
			status, outcome = http.StatusServiceUnavailable, "unavailable"
			body.Error.Code, body.Error.Message = "unavailable", "forecast data is loading; retry shortly"
			noStore.set(w.Header())
			w.Header().Set("Retry-After", "30")
		default:
			status, outcome = http.StatusInternalServerError, "error"
			body.Error.Code, body.Error.Message = "internal", "internal error"
			noStore.set(w.Header())
			h.log.Error("api call failed", "tool", tool, "err", err)
		}
		writeJSON(w, status, body)
		h.meter.Record(tool, surface, class, outcome, 0, time.Since(start))
		return
	}

	if r.Method == http.MethodPost || tool == "aurora" {
		// A POST answer is not cacheable by URL, so it is not cached at all.
		noStore.set(w.Header())
		writeJSON(w, http.StatusOK, env)
		h.meter.Record(tool, surface, class, "ok", env.Results, time.Since(start))
		return
	}
	CacheControl.set(w.Header())
	w.Header().Set("ETag", env.ETag)
	if match := r.Header.Get("If-None-Match"); match != "" && etagMatches(match, env.ETag) {
		w.WriteHeader(http.StatusNotModified)
		h.meter.Record(tool, surface, class, "not_modified", 0, time.Since(start))
		return
	}
	writeJSON(w, http.StatusOK, env)
	h.meter.Record(tool, surface, class, "ok", env.Results, time.Since(start))
}

func etagMatches(header, etag string) bool {
	for _, part := range strings.Split(header, ",") {
		part = strings.TrimPrefix(strings.TrimSpace(part), "W/")
		if part == etag || part == "*" {
			return true
		}
	}
	return false
}

func (h *Handler) static(body []byte, contentType string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentType)
		cacheControlStatic.set(w.Header())
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		_, _ = w.Write(body)
	}
}

func (h *Handler) methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body errorBody
		body.Meta = connector.Meta{APIVersion: connector.APIVersion, GeneratedAt: time.Now().UTC().Format(time.RFC3339), MethodURL: h.svc.MethodURL, Sources: []connector.Source{}}
		body.Error.Code, body.Error.Message = "method_not_allowed", "use "+allow+"; see "+h.specURL
		w.Header().Set("Allow", allow)
		cacheControlError.set(w.Header())
		writeJSON(w, http.StatusMethodNotAllowed, body)
	}
}

func (h *Handler) notFound(w http.ResponseWriter, r *http.Request) {
	var body errorBody
	body.Meta = connector.Meta{APIVersion: connector.APIVersion, GeneratedAt: time.Now().UTC().Format(time.RFC3339), MethodURL: h.svc.MethodURL, Sources: []connector.Source{}}
	body.Error.Code, body.Error.Message = "not_found", "no such endpoint; see "+h.specURL
	cacheControlError.set(w.Header())
	writeJSON(w, http.StatusNotFound, body)
}

var bufPool = sync.Pool{New: func() any { return new(bytes.Buffer) }}

func writeJSON(w http.ResponseWriter, status int, v any) {
	buf := bufPool.Get().(*bytes.Buffer)
	buf.Reset()
	defer bufPool.Put(buf)
	if err := json.NewEncoder(buf).Encode(v); err != nil {
		http.Error(w, `{"error":{"code":"internal","message":"encode failed"}}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(buf.Len()))
	w.WriteHeader(status)
	_, _ = w.Write(buf.Bytes())
}
