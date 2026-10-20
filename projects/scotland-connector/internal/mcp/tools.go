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
		Description: "Find the best places and nights to see the night sky near a point in Scotland. " +
			"Use this when someone asks where or when to go stargazing, see the Milky Way, or find dark, clear skies in Scotland, " +
			"or whether a particular night (night=YYYY-MM-DD) will be any good. " +
			"Ranks dark-sky sites (road-accessible, in the darkest light-pollution zones) by forecast clear-dark hours " +
			"(cloud under 10%, sun more than 12 degrees below the horizon). Each site leads with best_night: the night with the most " +
			"moonless clear-dark hours (moon below the horizon or under 10% lit), with its clear window and the moon's illumination; " +
			"then a short per-night breakdown and the site's historical clear-sky odds for the month. " +
			"Pass detail=hours only if you need every clear hour with the moon's altitude. " +
			"In midsummer, when Scotland gets no full darkness, clear_twilight_hours are the darkest available. " +
			"Give lat and lon in decimal degrees for the place the person means. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 150, "default": 50, "description": "Search radius in km."},
    "night": {"type": "string", "format": "date", "description": "One night to ask about, named by its evening's date (YYYY-MM-DD), within the next 7 nights. Use instead of nights."},
    "nights": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3, "description": "Nights to cover, starting tonight (from 09:00 local, the coming evening)."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "How many sites to return."},
    "detail": {"type": "string", "enum": ["summary", "hours"], "default": "summary", "description": "summary leads with best_night; hours adds every clear hour."}
  },
  "required": ["lat", "lon"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Dark-sky windows near a place"),
	},
	{
		Name:  "walk_windows",
		Title: "Walk weather windows",
		Description: "Find Scottish hill and coastal walks that fit a dry, calm daylight slot, near a point or by name. " +
			"Use this when someone asks where to go walking or hillwalking in Scotland, whether a day suits a walk " +
			"(date=YYYY-MM-DD, for example the coming Saturday), or whether a named walk is on (walk_name, for example \"Ben Nevis\"). " +
			"Ranks Walkhighlands walks by how many days have a good-weather slot long enough for the walk's estimated time " +
			"(daylight, at most 2 mm rain and wind at most 80 km/h per hour, one bad hour tolerated), then the earliest such day, then distance. " +
			"Each walk leads with best: its earliest doable day and slot with the wind, rain and temperature in it, " +
			"then its Walkhighlands url, start point, length, ascent and time, and a short per-day summary. " +
			"Each day says how much of it the hourly forecast covers (about two and a half days ahead); doable is null when it cannot decide. " +
			"Weather is forecast at the walk's start, not on summits; send people to the url for the route. " +
			"Give lat and lon in decimal degrees, or a walk_name, or both. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "walk_name": {"type": "string", "maxLength": 100, "description": "Words from a Walkhighlands walk name, for example \"Ben Nevis\" or \"Dun Deardail\". Accents optional."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 100, "default": 30, "description": "Search radius in km around the point, to each walk's start. Needs lat and lon."},
    "date": {"type": "string", "format": "date", "description": "One day to ask about (YYYY-MM-DD, UK date), up to 13 days ahead. Days past the hourly forecast come back with forecast_coverage none. Use instead of days."},
    "days": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3, "description": "Days to cover, starting today (UK time)."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "How many walks to return."},
    "max_duration_h": {"type": "number", "minimum": 0.5, "maximum": 24, "description": "Only walks whose estimated time is at most this many hours."},
    "max_ascent_m": {"type": "integer", "minimum": 1, "maximum": 3000, "description": "Only walks with at most this much ascent, in metres."},
    "detail": {"type": "string", "enum": ["summary", "hours"], "default": "summary", "description": "summary leads with best; hours adds the hourly weather."}
  },
  "anyOf": [{"required": ["lat", "lon"]}, {"required": ["walk_name"]}],
  "additionalProperties": false
}`),
		Annotations: readOnly("Walk weather windows"),
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
