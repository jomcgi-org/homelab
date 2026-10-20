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
			"clear_twilight_hours are the darkest available. Give lat and lon in decimal degrees for the place the person means. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 150, "default": 50, "description": "Search radius in km."},
    "nights": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3, "description": "Nights to cover, starting tonight (from 09:00 local, the coming evening)."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "How many sites to return."}
  },
  "required": ["lat", "lon"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Dark-sky windows near a place"),
	},
	{
		Name:  "walk_windows",
		Title: "Walk weather windows near a place",
		Description: "Find Scottish hill and coastal walks near a point that fit a dry, calm daylight slot over the next few days. " +
			"Use this when someone asks where or when to go walking or hillwalking in Scotland, or whether a walk is on this weekend. " +
			"Ranks Walkhighlands walks by how many days have a long enough good-weather slot for the walk's estimated time " +
			"(daylight, at most 2 mm rain and wind at most 80 km/h per hour, one bad hour tolerated), then the earliest such day, then distance. " +
			"Each walk carries its Walkhighlands url, start point, length, ascent and time, and per day the best slot and the hourly weather. " +
			"Each day says how much of it the forecast covers; doable is null when the forecast does not reach far enough to decide. " +
			"Weather is forecast at the walk's start, not on summits; send people to the url for the route. " +
			"Give lat and lon in decimal degrees for the place the person means. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 100, "default": 30, "description": "Search radius in km around the point, to each walk's start."},
    "days": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3, "description": "Days to cover, starting today (UK time). The hourly forecast reaches about two and a half days."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "How many walks to return."},
    "max_duration_h": {"type": "number", "minimum": 0.5, "maximum": 24, "description": "Only walks whose estimated time is at most this many hours."},
    "max_ascent_m": {"type": "integer", "minimum": 1, "maximum": 3000, "description": "Only walks with at most this much ascent, in metres."}
  },
  "required": ["lat", "lon"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Walk weather windows near a place"),
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
	case "walk_windows":
		var p connector.WalkWindowsParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.WalkWindows(p)
	}
	return nil, errUnknownTool
}
