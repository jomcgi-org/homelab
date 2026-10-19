// Package httpapi serves the connector's REST API under /v1.
package httpapi

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
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

// CacheControl for answers: forecasts move hourly, so the edge holds an
// answer for ten minutes, may serve it stale for an hour while revalidating,
// and keeps answering for a day if the origin is down. Browsers revalidate
// after a minute.
const CacheControl = "public, max-age=60, s-maxage=600, stale-while-revalidate=3600, stale-if-error=86400"

// cacheControlStatic is for the spec and the method page, which change only
// with a deploy.
const cacheControlStatic = "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400, stale-if-error=604800"

// cacheControlError lets the edge absorb repeated bad requests briefly.
const cacheControlError = "public, max-age=60, s-maxage=60"

const surface = "api"

// Handler serves /v1.
type Handler struct {
	svc   *connector.Service
	meter *meter.Meter
	log   *slog.Logger
	mux   *http.ServeMux
}

// New wires the routes.
func New(svc *connector.Service, m *meter.Meter, log *slog.Logger) *Handler {
	h := &Handler{svc: svc, meter: m, log: log, mux: http.NewServeMux()}
	h.mux.HandleFunc("GET /v1/places/resolve", h.resolvePlace)
	h.mux.HandleFunc("GET /v1/dark-sky-windows", h.darkSkyWindows)
	h.mux.HandleFunc("GET /v1/openapi.json", h.static(openAPI, "application/json"))
	h.mux.HandleFunc("GET /v1/method", h.static(method, "text/markdown; charset=utf-8"))
	h.mux.HandleFunc("/v1/", h.notFound)
	return h
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	h.mux.ServeHTTP(w, r)
}

func (h *Handler) resolvePlace(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	env, err := h.svc.ResolvePlace(connector.ResolvePlaceParams{Query: r.URL.Query().Get("q")})
	h.respond(w, r, "resolve_place", start, env, err)
}

func (h *Handler) darkSkyWindows(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	q := r.URL.Query()
	p := connector.DarkSkyParams{Place: q.Get("place")}
	var err error
	p.Lat, err = floatParam(q.Get("lat"), "lat", err)
	p.Lon, err = floatParam(q.Get("lon"), "lon", err)
	p.RadiusKm, err = floatParam(q.Get("radius_km"), "radius_km", err)
	p.Nights, err = intParam(q.Get("nights"), "nights", err)
	p.Limit, err = intParam(q.Get("limit"), "limit", err)
	var env *connector.Envelope
	if err == nil {
		env, err = h.svc.DarkSkyWindows(p)
	}
	h.respond(w, r, "dark_sky_windows", start, env, err)
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
			w.Header().Set("Cache-Control", cacheControlError)
		case errors.Is(err, connector.ErrUnavailable):
			status, outcome = http.StatusServiceUnavailable, "unavailable"
			body.Error.Code, body.Error.Message = "unavailable", "forecast data is loading; retry shortly"
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Retry-After", "30")
		default:
			status, outcome = http.StatusInternalServerError, "error"
			body.Error.Code, body.Error.Message = "internal", "internal error"
			w.Header().Set("Cache-Control", "no-store")
			h.log.Error("api call failed", "tool", tool, "err", err)
		}
		writeJSON(w, status, body)
		h.meter.Record(tool, surface, class, outcome, 0, time.Since(start))
		return
	}

	w.Header().Set("Cache-Control", CacheControl)
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
		w.Header().Set("Cache-Control", cacheControlStatic)
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		_, _ = w.Write(body)
	}
}

func (h *Handler) notFound(w http.ResponseWriter, r *http.Request) {
	var body errorBody
	body.Meta = connector.Meta{APIVersion: connector.APIVersion, GeneratedAt: time.Now().UTC().Format(time.RFC3339), MethodURL: h.svc.MethodURL, Sources: []connector.Source{}}
	body.Error.Code, body.Error.Message = "not_found", "no such endpoint; see /v1/openapi.json"
	w.Header().Set("Cache-Control", cacheControlError)
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
