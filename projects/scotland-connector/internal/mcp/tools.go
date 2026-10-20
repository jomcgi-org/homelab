package mcp

import (
	"encoding/json"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
)

// Tool is one MCP tool definition as tools/list returns it.
type Tool struct {
	Name        string          `json:"name"`
	Title       string          `json:"title"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
	Annotations Annotations     `json:"annotations"`
}

// Annotations are the behaviour hints the Claude directory requires.
type Annotations struct {
	Title           string `json:"title"`
	ReadOnlyHint    bool   `json:"readOnlyHint"`
	DestructiveHint bool   `json:"destructiveHint"`
	IdempotentHint  bool   `json:"idempotentHint"`
	OpenWorldHint   bool   `json:"openWorldHint"`
}

func readOnly(title string) Annotations {
	// Every tool reads a closed, in-memory dataset: nothing is written and
	// nothing outside the service is reached at call time.
	return Annotations{Title: title, ReadOnlyHint: true, DestructiveHint: false, IdempotentHint: true, OpenWorldHint: false}
}

// Tools is the fixed tool list, in a deterministic order.
var Tools = []Tool{
	{
		Name:  "dark_sky_windows",
		Title: "Dark-sky windows near a place",
		Description: "Find the best places to see the night sky near a point in Scotland over the next few nights. " +
			"Use this when someone asks where or when to go stargazing, see the Milky Way, or find dark, clear skies in Scotland. " +
			"Ranks dark-sky sites (road-accessible, in the darkest light-pollution zones) by forecast clear-dark hours " +
			"(cloud under 10%, sun more than 12 degrees below the horizon), with a per-night breakdown, the clear hours themselves, " +
			"and each site's historical clear-sky odds for the month. In midsummer, when Scotland gets no full darkness, " +
			"clear_twilight_hours are the darkest available. Give lat and lon, or a place name. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "place": {"type": "string", "maxLength": 100, "description": "Scottish place name, used instead of lat and lon, for example \"Aviemore\" or \"Inbhir Nis\"."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 150, "default": 50, "description": "Search radius in km."},
    "nights": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3, "description": "Nights to cover, starting tonight (from 09:00 local, the coming evening)."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "How many sites to return."}
  },
  "additionalProperties": false
}`),
		Annotations: readOnly("Dark-sky windows near a place"),
	},
	{
		Name:  "resolve_place",
		Title: "Resolve a Scottish place name",
		Description: "Turn a Scottish place name (English or Gaelic, accents optional) or a \"lat, lon\" pair into coordinates and a place type. " +
			"Use this to check which place a name refers to before calling dark_sky_windows, or when a name is ambiguous. " +
			"Returns up to 5 candidates, best first. Covers towns, villages and dark-sky places; if nothing matches, pass coordinates directly.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 100, "description": "Place name, or \"lat, lon\"."}
  },
  "required": ["query"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Resolve a Scottish place name"),
	},
}

// call decodes arguments and runs the tool.
func call(svc *connector.Service, name string, args json.RawMessage) (*connector.Envelope, error) {
	if len(args) == 0 || string(args) == "null" {
		args = json.RawMessage(`{}`)
	}
	switch name {
	case "dark_sky_windows":
		var p connector.DarkSkyParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.DarkSkyWindows(p)
	case "resolve_place":
		var p connector.ResolvePlaceParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.ResolvePlace(p)
	}
	return nil, errUnknownTool
}
