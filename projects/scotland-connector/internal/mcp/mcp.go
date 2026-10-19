// Package mcp serves the connector's tools over MCP Streamable HTTP.
//
// It is a dual-era, stateless server:
//
//   - Modern (2026-07-28): every request carries its protocol version and
//     client capabilities in params._meta, mirrored into the
//     MCP-Protocol-Version, Mcp-Method and Mcp-Name headers, which must agree
//     with the body. There is no handshake and no session.
//   - Legacy (2025-03-26 to 2025-11-25): clients open with initialize. The
//     server answers it and every later call statelessly; it never mints an
//     Mcp-Session-Id, which those revisions allow.
//
// Responses are always a single application/json object: no tool streams.
// GET and DELETE on the endpoint return 405.
package mcp

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
)

// ModernVersion is the stateless protocol revision.
const ModernVersion = "2026-07-28"

// LegacyVersions are the initialize-era revisions served, newest first.
var LegacyVersions = []string{"2025-11-25", "2025-06-18", "2025-03-26"}

// maxBody bounds a request; the largest legitimate call is a few hundred bytes.
const maxBody = 64 << 10

// JSON-RPC and MCP error codes.
const (
	codeParse               = -32700
	codeInvalidRequest      = -32600
	codeMethodNotFound      = -32601
	codeInvalidParams       = -32602
	codeInternal            = -32603
	codeHeaderMismatch      = -32020
	codeUnsupportedProtocol = -32022
)

const metaPrefix = "io.modelcontextprotocol/"

var errUnknownTool = errors.New("unknown tool")

// ServerInfo identifies the server.
type ServerInfo struct {
	Name    string `json:"name"`
	Title   string `json:"title,omitempty"`
	Version string `json:"version"`
}

// Handler serves the MCP endpoint.
type Handler struct {
	svc     *connector.Service
	meter   *meter.Meter
	log     *slog.Logger
	info    ServerInfo
	origins map[string]bool
}

// Instructions are returned by server/discover and initialize.
const Instructions = "Read-only answers to Scottish outdoor questions from forecast and climatology data. " +
	"Use dark_sky_windows for where and when to stargaze; use resolve_place when a place name is ambiguous. " +
	"Every result carries meta.sources: credit them when you present the data."

// New builds the handler. allowedOrigins lists the browser origins accepted;
// requests without an Origin header (every server-side connector) always pass.
func New(svc *connector.Service, m *meter.Meter, log *slog.Logger, info ServerInfo, allowedOrigins []string) *Handler {
	h := &Handler{svc: svc, meter: m, log: log, info: info, origins: map[string]bool{}}
	for _, o := range allowedOrigins {
		if o = strings.TrimSpace(o); o != "" {
			h.origins[strings.TrimRight(o, "/")] = true
		}
	}
	return h
}

type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// era is how one request is being served.
type era struct {
	modern  bool
	version string
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if origin := r.Header.Get("Origin"); origin != "" && !h.origins[strings.TrimRight(origin, "/")] && !h.origins["*"] {
		h.write(w, http.StatusForbidden, response{JSONRPC: "2.0", Error: &rpcError{Code: codeInvalidRequest, Message: "origin not allowed"}})
		return
	}
	if r.Method != http.MethodPost {
		// No standalone SSE stream and no sessions to DELETE in either era
		// this server speaks.
		w.Header().Set("Allow", http.MethodPost)
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		h.write(w, http.StatusRequestEntityTooLarge, response{JSONRPC: "2.0", Error: &rpcError{Code: codeInvalidRequest, Message: "request too large"}})
		return
	}
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", Error: &rpcError{Code: codeInvalidRequest, Message: "batch requests are not supported"}})
		return
	}
	var req request
	if err := json.Unmarshal(trimmed, &req); err != nil {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", Error: &rpcError{Code: codeParse, Message: "parse error"}})
		return
	}
	if req.JSONRPC != "2.0" || req.Method == "" {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", ID: req.ID, Error: &rpcError{Code: codeInvalidRequest, Message: "not a JSON-RPC 2.0 request"}})
		return
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		// A notification (notifications/initialized from legacy clients is
		// the only one expected). Accept and ignore: there is no state.
		w.WriteHeader(http.StatusAccepted)
		return
	}

	e, status, rerr := negotiate(r, req)
	if rerr != nil {
		h.write(w, status, response{JSONRPC: "2.0", ID: req.ID, Error: rerr})
		return
	}
	result, status, rerr := h.dispatch(r, e, req)
	if rerr != nil {
		h.write(w, status, response{JSONRPC: "2.0", ID: req.ID, Error: rerr})
		return
	}
	if e.modern {
		w.Header().Set("MCP-Protocol-Version", e.version)
	}
	h.write(w, http.StatusOK, response{JSONRPC: "2.0", ID: req.ID, Result: result})
}

// negotiate picks the era for one request and validates its metadata.
func negotiate(r *http.Request, req request) (era, int, *rpcError) {
	headerVersion := r.Header.Get("MCP-Protocol-Version")
	if req.Method == "initialize" {
		return era{version: legacyVersion(req.Params)}, 0, nil
	}

	var params struct {
		Meta map[string]json.RawMessage `json:"_meta"`
		Name *string                    `json:"name"`
	}
	if len(req.Params) > 0 {
		if err := json.Unmarshal(req.Params, &params); err != nil {
			return era{}, http.StatusBadRequest, &rpcError{Code: codeInvalidParams, Message: "params must be an object"}
		}
	}
	rawVersion, modern := params.Meta[metaPrefix+"protocolVersion"]
	if !modern {
		// Legacy era: the version header (absent before 2025-06-18) names it.
		switch {
		case headerVersion == "":
			return era{version: "2025-03-26"}, 0, nil
		case isLegacy(headerVersion):
			return era{version: headerVersion}, 0, nil
		case headerVersion == ModernVersion:
			return era{}, http.StatusBadRequest, &rpcError{Code: codeInvalidParams, Message: "missing _meta." + metaPrefix + "protocolVersion"}
		default:
			return era{}, http.StatusBadRequest, unsupported(headerVersion)
		}
	}

	var version string
	if err := json.Unmarshal(rawVersion, &version); err != nil {
		return era{}, http.StatusBadRequest, &rpcError{Code: codeInvalidParams, Message: "protocolVersion must be a string"}
	}
	if headerVersion == "" {
		return era{}, http.StatusBadRequest, &rpcError{Code: codeHeaderMismatch, Message: "Header mismatch: MCP-Protocol-Version header is required"}
	}
	if headerVersion != version {
		return era{}, http.StatusBadRequest, &rpcError{Code: codeHeaderMismatch, Message: fmt.Sprintf("Header mismatch: MCP-Protocol-Version header value '%s' does not match body value '%s'", headerVersion, version)}
	}
	if version != ModernVersion {
		return era{}, http.StatusBadRequest, unsupported(version)
	}
	if _, ok := params.Meta[metaPrefix+"clientCapabilities"]; !ok {
		return era{}, http.StatusBadRequest, &rpcError{Code: codeInvalidParams, Message: "missing _meta." + metaPrefix + "clientCapabilities"}
	}
	if m := r.Header.Get("Mcp-Method"); m != req.Method {
		return era{}, http.StatusBadRequest, &rpcError{Code: codeHeaderMismatch, Message: fmt.Sprintf("Header mismatch: Mcp-Method header value '%s' does not match body value '%s'", m, req.Method)}
	}
	if req.Method == "tools/call" {
		raw, present := r.Header["Mcp-Name"]
		if !present || len(raw) == 0 {
			return era{}, http.StatusBadRequest, &rpcError{Code: codeHeaderMismatch, Message: "Header mismatch: Mcp-Name header is required for tools/call"}
		}
		name, ok := decodeHeaderValue(raw[0])
		if !ok || params.Name == nil || name != *params.Name {
			body := ""
			if params.Name != nil {
				body = *params.Name
			}
			return era{}, http.StatusBadRequest, &rpcError{Code: codeHeaderMismatch, Message: fmt.Sprintf("Header mismatch: Mcp-Name header value '%s' does not match body value '%s'", raw[0], body)}
		}
	}
	return era{modern: true, version: version}, 0, nil
}

func unsupported(requested string) *rpcError {
	return &rpcError{
		Code:    codeUnsupportedProtocol,
		Message: "Unsupported protocol version",
		Data: map[string]any{
			"supported": append([]string{ModernVersion}, LegacyVersions...),
			"requested": requested,
		},
	}
}

func isLegacy(v string) bool {
	for _, l := range LegacyVersions {
		if v == l {
			return true
		}
	}
	return false
}

// legacyVersion answers initialize: the client's version if served, else the
// newest legacy version, which the client may accept or disconnect from.
func legacyVersion(params json.RawMessage) string {
	var p struct {
		ProtocolVersion string `json:"protocolVersion"`
	}
	_ = json.Unmarshal(params, &p)
	if isLegacy(p.ProtocolVersion) {
		return p.ProtocolVersion
	}
	return LegacyVersions[0]
}

// decodeHeaderValue undoes the =?base64?...?= sentinel encoding.
func decodeHeaderValue(v string) (string, bool) {
	if strings.HasPrefix(v, "=?base64?") && strings.HasSuffix(v, "?=") {
		b, err := base64.StdEncoding.DecodeString(strings.TrimSuffix(strings.TrimPrefix(v, "=?base64?"), "?="))
		if err != nil {
			return "", false
		}
		return string(b), true
	}
	return v, true
}

func (h *Handler) dispatch(r *http.Request, e era, req request) (any, int, *rpcError) {
	notFound := http.StatusOK
	if e.modern {
		notFound = http.StatusNotFound
	}
	switch req.Method {
	case "initialize":
		return map[string]any{
			"protocolVersion": e.version,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      h.info,
			"instructions":    Instructions,
		}, 0, nil
	case "ping":
		return h.result(e, map[string]any{}), 0, nil
	case "server/discover":
		if !e.modern {
			return nil, notFound, &rpcError{Code: codeMethodNotFound, Message: "Method not found: server/discover"}
		}
		return h.result(e, map[string]any{
			"supportedVersions": []string{ModernVersion},
			"capabilities":      map[string]any{"tools": map[string]any{}},
			"instructions":      Instructions,
			"ttlMs":             3600000,
			"cacheScope":        "public",
		}), 0, nil
	case "tools/list":
		out := map[string]any{"tools": Tools}
		if e.modern {
			out["ttlMs"] = 3600000
			out["cacheScope"] = "public"
		}
		return h.result(e, out), 0, nil
	case "tools/call":
		return h.toolsCall(r, e, req.Params)
	}
	return nil, notFound, &rpcError{Code: codeMethodNotFound, Message: "Method not found: " + req.Method}
}

// result adds the modern per-response fields.
func (h *Handler) result(e era, out map[string]any) map[string]any {
	if e.modern {
		out["resultType"] = "complete"
		out["_meta"] = map[string]any{metaPrefix + "serverInfo": h.info}
	}
	return out
}

func (h *Handler) toolsCall(r *http.Request, e era, raw json.RawMessage) (any, int, *rpcError) {
	start := time.Now()
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if err := json.Unmarshal(raw, &p); err != nil || p.Name == "" {
		return nil, http.StatusBadRequest, &rpcError{Code: codeInvalidParams, Message: "tools/call needs a tool name"}
	}
	class := meter.ClientClass(r)
	env, err := call(h.svc, p.Name, p.Arguments)
	if errors.Is(err, errUnknownTool) {
		h.meter.Record("unknown", "mcp", class, "unknown_tool", 0, time.Since(start))
		status := http.StatusOK
		if e.modern {
			status = http.StatusBadRequest
		}
		return nil, status, &rpcError{Code: codeInvalidParams, Message: "Unknown tool: " + p.Name}
	}
	if err != nil {
		// Tool execution errors go back in the result so the model can
		// correct its arguments and retry.
		outcome, msg := "invalid", err.Error()
		var pe *connector.ParamError
		switch {
		case errors.As(err, &pe):
			msg = "Invalid " + pe.Param + ": " + pe.Message
		case errors.Is(err, connector.ErrUnavailable):
			outcome, msg = "unavailable", "Forecast data is still loading; retry in a minute."
		case errors.Is(err, errBadArguments):
		default:
			outcome, msg = "error", "Internal error."
			h.log.Error("mcp tool failed", "tool", p.Name, "err", err)
		}
		h.meter.Record(p.Name, "mcp", class, outcome, 0, time.Since(start))
		return h.result(e, map[string]any{
			"content": []map[string]any{{"type": "text", "text": msg}},
			"isError": true,
		}), 0, nil
	}
	text, err := json.Marshal(env)
	if err != nil {
		return nil, http.StatusInternalServerError, &rpcError{Code: codeInternal, Message: "encode failed"}
	}
	h.meter.Record(p.Name, "mcp", class, "ok", env.Results, time.Since(start))
	return h.result(e, map[string]any{
		"content":           []map[string]any{{"type": "text", "text": string(text)}},
		"structuredContent": env,
		"isError":           false,
	}), 0, nil
}

var errBadArguments = errors.New("bad arguments")

// strictDecode rejects unknown argument names so a typo reaches the model as
// an error instead of a silently ignored filter.
func strictDecode(args json.RawMessage, v any) error {
	dec := json.NewDecoder(bytes.NewReader(args))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("%w: %s", errBadArguments, strings.TrimPrefix(err.Error(), "json: "))
	}
	return nil
}

func (h *Handler) write(w http.ResponseWriter, status int, resp response) {
	b, err := json.Marshal(resp)
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.WriteHeader(status)
	_, _ = w.Write(b)
}
