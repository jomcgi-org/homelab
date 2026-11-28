package artifacts

import (
	"bytes"
	"context"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
)

// Versions are the MCP revisions served, newest first. The server is
// stateless: it answers initialize and never mints an Mcp-Session-Id, which
// these revisions allow. Each answer is one application/json object.
var Versions = []string{"2025-11-25", "2025-06-18", "2025-03-26"}

// maxBody bounds a request: a 2 MiB artifact in base64 or escaped JSON.
const maxBody = 8 << 20

const (
	codeParse          = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
)

// Verifier checks the actor JWT Substrate's egress gateway injects.
type Verifier interface {
	Verify(ctx context.Context, raw string) (actorjwt.Identity, error)
}

// Handler serves the MCP endpoint.
type Handler struct {
	Store    *Store
	Verifier Verifier
	Log      *slog.Logger
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
}

type response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

const instructions = "Publish files for Joe and give him the link. put writes one file under the Discord server and channel of the " +
	"current message and returns its https://artifact.jomcgi.dev URL. With the current message's server ID, every member of " +
	"that server can open it; with server private, only Joe can. Use private when Joe asks for a private file or the file " +
	"holds a direct message or text from a channel that some members cannot read. HTML pages run sandboxed " +
	"with an opaque origin and no network: put CSS, scripts, images and data inline (data: URLs). A page cannot load " +
	"a CDN library, another artifact or any URL, and cannot keep cookies or storage. list and delete work in the same server and channel. " +
	"Only when Joe asks for a share link for people outside the server, end your answer with a fenced share block, for example " +
	"```share\n{\"path\": \"report.html\", \"ttl\": \"24h\"}\n``` (a file of this channel; ttl 1m to 7d, 24h by default). " +
	"You cannot make the link: the bridge asks Joe to confirm with a reaction, then posts it."

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	// Browsers never call this service; only the egress gateway does.
	if r.Header.Get("Origin") != "" {
		h.write(w, http.StatusForbidden, response{JSONRPC: "2.0", Error: &rpcError{codeInvalidRequest, "origin not allowed"}})
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	raw, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok {
		w.Header().Set("WWW-Authenticate", "Bearer")
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	id, err := h.Verifier.Verify(r.Context(), raw)
	if err != nil {
		reason := "invalid"
		var je *actorjwt.Error
		if errors.As(err, &je) {
			reason = je.Reason
		}
		h.Log.Warn("caller rejected", "reason", reason)
		w.Header().Set("WWW-Authenticate", "Bearer")
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		h.write(w, http.StatusRequestEntityTooLarge, response{JSONRPC: "2.0", Error: &rpcError{codeInvalidRequest, "request too large"}})
		return
	}
	body = bytes.TrimSpace(body)
	if len(body) > 0 && body[0] == '[' {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", Error: &rpcError{codeInvalidRequest, "batch requests are not supported"}})
		return
	}
	var req request
	if err := json.Unmarshal(body, &req); err != nil {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", Error: &rpcError{codeParse, "parse error"}})
		return
	}
	if req.JSONRPC != "2.0" || req.Method == "" {
		h.write(w, http.StatusBadRequest, response{JSONRPC: "2.0", Error: &rpcError{codeInvalidRequest, "not a JSON-RPC 2.0 request"}})
		return
	}
	if req.ID == nil {
		// A notification, such as notifications/initialized: no state to keep.
		w.WriteHeader(http.StatusAccepted)
		return
	}
	result, rerr := h.dispatch(r.Context(), id, req)
	if rerr != nil {
		h.write(w, http.StatusOK, response{JSONRPC: "2.0", ID: req.ID, Error: rerr})
		return
	}
	h.write(w, http.StatusOK, response{JSONRPC: "2.0", ID: req.ID, Result: result})
}

func (h *Handler) dispatch(ctx context.Context, id actorjwt.Identity, req request) (any, *rpcError) {
	switch req.Method {
	case "initialize":
		var p struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &p)
		version := Versions[0]
		for _, v := range Versions {
			if v == p.ProtocolVersion {
				version = v
			}
		}
		return map[string]any{
			"protocolVersion": version,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      map[string]string{"name": "bosun-artifacts", "version": "1"},
			"instructions":    instructions,
		}, nil
	case "ping":
		return map[string]any{}, nil
	case "tools/list":
		return map[string]any{"tools": Tools}, nil
	case "tools/call":
		return h.call(ctx, id, req.Params), nil
	}
	return nil, &rpcError{codeMethodNotFound, "Method not found: " + req.Method}
}

func (h *Handler) call(ctx context.Context, id actorjwt.Identity, raw json.RawMessage) any {
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return toolError("tools/call needs a tool name and arguments")
	}
	var (
		out any
		err error
		key string
	)
	switch p.Name {
	case "put":
		var a PutArgs
		if err = strictDecode(p.Arguments, &a); err == nil {
			key = a.Server + "/" + a.Channel + "/" + a.Path
			out, err = h.Store.Put(ctx, a)
		}
	case "template":
		var a struct {
			Example string `json:"example"`
		}
		if err = strictDecode(p.Arguments, &a); err == nil {
			out, err = templateResult(a.Example)
		}
	case "list":
		var a Place
		if err = strictDecode(p.Arguments, &a); err == nil {
			key = a.Server + "/" + a.Channel + "/"
			out, err = h.Store.List(ctx, a)
		}
	case "delete":
		var a DeleteArgs
		if err = strictDecode(p.Arguments, &a); err == nil {
			key = a.Server + "/" + a.Channel + "/" + a.Path
			out, err = h.Store.Delete(ctx, a)
		}
	default:
		return toolError("Unknown tool: " + p.Name)
	}
	var pe *ParamError
	switch {
	case errors.As(err, &pe):
		h.Log.Info("tool refused", "tool", p.Name, "actor", id.Subject, "param", pe.Param)
		return toolError("Invalid " + pe.Error())
	case err != nil:
		h.Log.Error("tool failed", "tool", p.Name, "actor", id.Subject, "key", key, "err", err)
		return toolError("Storage error; try again.")
	}
	h.Log.Info("tool ok", "tool", p.Name, "actor", id.Subject, "key", key)
	text, _ := json.Marshal(out)
	return map[string]any{
		"content":           []map[string]any{{"type": "text", "text": string(text)}},
		"structuredContent": out,
		"isError":           false,
	}
}

func toolError(msg string) map[string]any {
	return map[string]any{"content": []map[string]any{{"type": "text", "text": msg}}, "isError": true}
}

// strictDecode rejects unknown argument names so a typo reaches the model.
func strictDecode(args json.RawMessage, v any) error {
	if len(args) == 0 {
		args = json.RawMessage("{}")
	}
	dec := json.NewDecoder(bytes.NewReader(args))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return &ParamError{"arguments", strings.TrimPrefix(err.Error(), "json: ")}
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

// Tool is one MCP tool definition.
type Tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
	Annotations map[string]any  `json:"annotations"`
}

const placeSchema = `"server":{"type":"string","pattern":"^([0-9]{15,21}|private)$","description":"Discord server (guild) ID of the current message, so the members of that server can open the file; or private, so only Joe can."},` +
	`"channel":{"type":"string","pattern":"^[0-9]{15,21}$","description":"Discord channel ID of the current message."}`

const pathSchema = `"path":{"type":"string","maxLength":200,"description":"File path under the channel, e.g. report.html or charts/q3.png. Letters, digits, '.', '_', '-'; up to 5 segments; extension .html .md .txt .csv .json .svg or .png."}`

// pageTemplate is the house HTML page Bosun starts an artifact from. It
// loads the house design system (house-2.css and house-2.js) and the pinned
// libraries from /_lib/ (projects/factory/discord/libs).
//
//go:embed template.html
var pageTemplate string

// designBrief tells the model how to use the design system.
//
//go:embed brief.md
var designBrief string

// examplePages are whole pages the model can adapt, by name.
//
//go:embed examples/*.html
var examplePages embed.FS

// exampleNames lists the example pages in the order the brief names them.
var exampleNames = []string{"explainer", "report", "comparison"}

// templateResult returns the brief, the base page and the example names, or
// one example page.
func templateResult(example string) (any, error) {
	if example == "" {
		return map[string]any{"brief": designBrief, "html": pageTemplate, "examples": exampleNames}, nil
	}
	for _, name := range exampleNames {
		if name == example {
			page, err := examplePages.ReadFile("examples/" + name + ".html")
			if err != nil {
				return nil, err
			}
			return map[string]any{"example": name, "html": string(page)}, nil
		}
	}
	return nil, &ParamError{"example", "must be one of " + strings.Join(exampleNames, ", ")}
}

// Tools is the fixed tool list.
var Tools = []Tool{
	{
		Name: "put",
		Description: fmt.Sprintf("Publish one file and return its URL for Joe. Replaces a file at the same path. At most %d bytes. "+
			"HTML runs sandboxed with no network: inline every style, script and image; scripts, styles and fonts may load only from /_lib/, as the template tool's pages do; no CDN or remote URL loads. Start an HTML page from the template tool and follow its brief. For an HTML page the result can list warnings: no /_lib/house-N.css link, emoji, or a :root rule with its own colour variables. The file is stored anyway; fix each warning and put the page again. PNG content is base64 with encoding base64; other types are UTF-8 text.", MaxBytes),
		InputSchema: json.RawMessage(`{"type":"object","properties":{` + placeSchema + `,` + pathSchema + `,` +
			`"content":{"type":"string","description":"File content."},` +
			`"encoding":{"type":"string","enum":["utf-8","base64"],"default":"utf-8"}},` +
			`"required":["server","channel","path","content"],"additionalProperties":false}`),
		Annotations: map[string]any{"title": "Publish an artifact", "readOnlyHint": false, "destructiveHint": true, "idempotentHint": true, "openWorldHint": false},
	},
	{
		Name:        "template",
		Description: "Return the house design system for an artifact page: a design brief (read it first), the base page, and the names of example pages. Pass example to get one example page (an explainer, a report or a comparison) to adapt. Pages load the design system and Mermaid, Vega-Lite and Lucide icons from /_lib/. Fill a page in and publish it with put.",
		InputSchema: json.RawMessage(`{"type":"object","properties":{"example":{"type":"string","enum":["explainer","report","comparison"],"description":"An example page to return instead of the brief and the base page."}},"additionalProperties":false}`),
		Annotations: map[string]any{"title": "Get the page template", "readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false},
	},
	{
		Name:        "list",
		Description: "List the files published in this Discord channel, with their URLs.",
		InputSchema: json.RawMessage(`{"type":"object","properties":{` + placeSchema + `},"required":["server","channel"],"additionalProperties":false}`),
		Annotations: map[string]any{"title": "List artifacts", "readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false},
	},
	{
		Name:        "delete",
		Description: "Delete one published file in this Discord channel.",
		InputSchema: json.RawMessage(`{"type":"object","properties":{` + placeSchema + `,` + pathSchema + `},"required":["server","channel","path"],"additionalProperties":false}`),
		Annotations: map[string]any{"title": "Delete an artifact", "readOnlyHint": false, "destructiveHint": true, "idempotentHint": true, "openWorldHint": false},
	},
}
