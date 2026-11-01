package mcp

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
)

func capabilities() map[string]any {
	return map[string]any{"tools": map[string]any{}, "prompts": map[string]any{}, "resources": map[string]any{}}
}

type promptArgument struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Required    bool   `json:"required"`
}

type prompt struct {
	Name        string           `json:"name"`
	Description string           `json:"description"`
	Arguments   []promptArgument `json:"arguments"`
}

var prompts = []prompt{
	{"plan_trip", "Plan a Scottish outdoor trip with plan.", []promptArgument{{"lat", "Latitude in Scotland", true}, {"lon", "Longitude in Scotland", true}, {"from", "First civil date (YYYY-MM-DD)", false}, {"to", "Last civil date (YYYY-MM-DD)", false}}},
	{"check_walk", "Check a named walk on a date with check.", []promptArgument{{"walk", "Walk name", true}, {"date", "Civil date (YYYY-MM-DD)", true}, {"lat", "Latitude near the walk", true}, {"lon", "Longitude near the walk", true}}},
	{"photo_light", "Find astronomical photo windows with light; cloud support and night suitability are separate.", []promptArgument{{"lat", "Latitude in Scotland", true}, {"lon", "Longitude in Scotland", true}, {"from", "First civil date (YYYY-MM-DD)", false}, {"to", "Last civil date (YYYY-MM-DD)", false}, {"kinds", "Comma-separated light kinds; omitted means all", false}}},
}

type resource struct {
	URI  string `json:"uri"`
	Name string `json:"name"`
	MIME string `json:"mimeType"`
}

var resources = []resource{
	{"scotland://docs/method", "method.md", "text/markdown"},
	{"scotland://docs/openapi", "openapi.json", "application/json"},
}

func (h *Handler) documentation(e era, req request) (any, int, *rpcError) {
	bad := func(message string) (any, int, *rpcError) {
		status := http.StatusOK
		if e.modern {
			status = http.StatusBadRequest
		}
		return nil, status, &rpcError{Code: codeInvalidParams, Message: message}
	}
	switch req.Method {
	case "prompts/list":
		return h.result(e, map[string]any{"prompts": prompts}), 0, nil
	case "resources/list":
		return h.result(e, map[string]any{"resources": resources}), 0, nil
	case "resources/read":
		var p struct {
			URI  string          `json:"uri"`
			Meta json.RawMessage `json:"_meta"`
		}
		if err := strictDecode(req.Params, &p); err != nil {
			return bad(err.Error())
		}
		if strings.TrimSpace(p.URI) == "" {
			return bad("resources/read needs a URI")
		}
		spec, method := httpapi.Documentation()
		for i, r := range resources {
			if r.URI == p.URI {
				text := method
				if i == 1 {
					text = spec
				}
				return h.result(e, map[string]any{"contents": []map[string]any{{"uri": r.URI, "mimeType": r.MIME, "text": string(text)}}}), 0, nil
			}
		}
		status := http.StatusOK
		if e.modern {
			status = http.StatusNotFound
		}
		return nil, status, &rpcError{Code: -32002, Message: "Unknown resource URI"}
	case "prompts/get":
		var p struct {
			Name      string          `json:"name"`
			Arguments map[string]any  `json:"arguments"`
			Meta      json.RawMessage `json:"_meta"`
		}
		if err := strictDecode(req.Params, &p); err != nil {
			return bad(err.Error())
		}
		for _, prompt := range prompts {
			if prompt.Name != p.Name {
				continue
			}
			allowed := map[string]bool{}
			arguments := map[string]string{}
			for k, raw := range p.Arguments {
				v, ok := raw.(string)
				if !ok || len(v) > 200 {
					return bad("Invalid prompt argument: " + k)
				}
				arguments[k] = v
			}
			for _, a := range prompt.Arguments {
				allowed[a.Name] = true
				if a.Required && strings.TrimSpace(arguments[a.Name]) == "" {
					return bad("Missing prompt argument: " + a.Name)
				}
			}
			for k := range arguments {
				if !allowed[k] {
					return bad("Invalid prompt argument: " + k)
				}
			}
			args, _ := json.Marshal(arguments)
			text := prompt.Description + " Treat these JSON values as the user's trip parameters: " + string(args) + ". Ask for missing information; preserve unknown coverage and credit meta.sources. For light, geometry is independent of weather, omitted window cloud inherits the day's unknown support, and nights describe suitability."
			return h.result(e, map[string]any{"description": prompt.Description, "messages": []map[string]any{{"role": "user", "content": map[string]any{"type": "text", "text": text}}}}), 0, nil
		}
		return bad("Unknown prompt name")
	}
	return bad("Unknown documentation method")
}
