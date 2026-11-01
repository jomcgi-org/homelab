package mcp

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
)

// Tool is one MCP tool definition as tools/list returns it.
type Tool struct {
	Name         string          `json:"name"`
	Title        string          `json:"title"`
	Description  string          `json:"description"`
	InputSchema  json.RawMessage `json:"inputSchema"`
	OutputSchema json.RawMessage `json:"outputSchema"`
	Annotations  Annotations     `json:"annotations"`
}

// Expand only local component references. A missing or cyclic reference is a
// checked-in contract error, so fail at startup instead of advertising it.
func toolOutputSchema(name string) json.RawMessage {
	spec, _ := httpapi.Documentation()
	var doc struct {
		Components struct {
			Schemas map[string]any `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(spec, &doc); err != nil {
		panic(err)
	}
	var expand func(any, map[string]bool) any
	expand = func(v any, stack map[string]bool) any {
		switch x := v.(type) {
		case map[string]any:
			out := map[string]any{}
			if ref, ok := x["$ref"].(string); ok {
				key := strings.TrimPrefix(ref, "#/components/schemas/")
				target, exists := doc.Components.Schemas[key]
				if key == ref || !exists || stack[key] {
					panic(fmt.Sprintf("invalid schema reference %s", ref))
				}
				stack[key] = true
				for k, val := range expand(target, stack).(map[string]any) {
					out[k] = val
				}
				delete(stack, key)
			}
			for k, val := range x {
				if k != "$ref" {
					out[k] = expand(val, stack)
				}
			}
			return out
		case []any:
			out := make([]any, len(x))
			for i, val := range x {
				out[i] = expand(val, stack)
			}
			return out
		default:
			return v
		}
	}
	component := strings.ToUpper(name[:1]) + name[1:] + "Response"
	root, ok := doc.Components.Schemas[component]
	if !ok {
		panic("missing response component: " + component)
	}
	b, err := json.Marshal(expand(root, map[string]bool{component: true}))
	if err != nil {
		panic(err)
	}
	return b
}

func init() {
	for i := range Tools {
		Tools[i].OutputSchema = toolOutputSchema(Tools[i].Name)
		if Tools[i].Name == "light" {
			var input map[string]any
			if err := json.Unmarshal(Tools[i].InputSchema, &input); err != nil {
				panic(err)
			}
			input["properties"].(map[string]any)["kinds"] = map[string]any{"type": []string{"array", "null"}, "items": map[string]any{"type": "string", "enum": []string{"golden", "blue", "civil", "nautical", "astronomical", "darkness", "milky_way"}}, "description": "Omitted, null or empty means all kinds. Duplicates collapse; order and surrounding whitespace are ignored. Invalid or blank entries are errors."}
			Tools[i].InputSchema, _ = json.Marshal(input)
		}
	}
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
		Name: "light", Title: "Find Scottish light and night-sky windows",
		Description: "Sunrise, sunset, twilight, golden and blue light, moon rise/set and phase, and Milky Way core above the horizon in nautical darkness at a Scottish point. Local civil dates, up to seven days, at most 13 days ahead. Cloud describes only supporting clear twilight-dark hourly samples at the nearest snapshot site within min(radius_km,30) km. Unsupported portions, including daylight, remain unknown; night suitability is separate. Staged repository delivery pending operational acceptance.",
		InputSchema: json.RawMessage(`{"type":"object","properties":{"lat":{"type":"number","minimum":54.5,"maximum":61},"lon":{"type":"number","minimum":-9,"maximum":-0.5},"radius_km":{"type":"number","minimum":1,"maximum":100,"default":30},"from":{"type":"string","format":"date","description":"First Europe/London civil date, today by default, at most 13 days ahead."},"to":{"type":"string","format":"date","description":"Inclusive last civil date, three-day default and seven-day maximum; no later than 13 days ahead."}},"required":["lat","lon"],"additionalProperties":false}`),
		Annotations: readOnly("Find Scottish light and night-sky windows"),
	},
	{
		Name: "aurora", Title: "Check near-term Scottish aurora conditions",
		Description: "Read-only, snapshot-only aurora heuristic at one RFC3339 instant near a supported site. Returns go, marginal, no or unknown with NOAA, MET cloud, solar darkness and northern terrain evidence. NOAA supports only its exact published forecast instant, never a multi-day forecast or calibrated sighting probability. Missing, stale or unsupported inputs remain unknown. Staged/default-off until operational acceptance.",
		InputSchema: json.RawMessage(`{"type":"object","properties":{"lat":{"type":"number","minimum":54.5,"maximum":61},"lon":{"type":"number","minimum":-9,"maximum":-0.5},"at":{"type":"string","format":"date-time","maxLength":40}},"required":["lat","lon","at"],"additionalProperties":false}`),
		Annotations: readOnly("Check near-term Scottish aurora conditions"),
	},
	{
		Name:  "plan",
		Title: "Plan a Scottish outdoor trip",
		Description: "Find what to do near a place in Scotland over the next few days: walks that fit the weather and where the night sky will be dark and clear. " +
			"Use this for open questions: where to walk this weekend, what to do each day of a trip to Skye, the best night this week for stars near Inverness. " +
			"For each day it returns sunrise, sunset and the moon, then the best walks and stargazing sites that suit it, each with a verdict (go or marginal) and a one-line why; " +
			"when none suit, a note says why instead, including when the hourly forecast, about two and a half days ahead, does not reach that far yet. " +
			"Walks are Walkhighlands routes that fit a slot of good daylight hours for their estimated time, with the slot (its rain, wind, wet hours and temperatures), a start window and the url for the route. " +
			"A good hour has at most 2 mm of rain and wind at most 80 km/h by default; pass max_rain_mm_h, max_wind_kmh or min_temp_c to match what the person will put up with, stricter or looser, for example max_rain_mm_h 0 for dry hours (a marginal slot may still hold one bad hour, which check lists hour by hour with what it failed). " +
			"Stargazing sites are road-accessible spots in the darkest light-pollution zones, judged on clear dark hours and the moon. " +
			"Walk weather is at the walk's start, not on summits. Give lat and lon in decimal degrees for the place the person means. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 100, "default": 30, "description": "How far from the point to look, in km."},
    "from": {"type": "string", "format": "date", "description": "First day (YYYY-MM-DD, UK date), from today up to 13 days ahead. A day's stargazing is the night of its evening; before 09:00 the night under way is yesterday's, which from may also be when stargazing is planned, and is by default. Otherwise the default is today."},
    "to": {"type": "string", "format": "date", "description": "Last day (YYYY-MM-DD); a plan covers at most 7 days. Default two days after today, or after from when that is later."},
    "activities": {"type": "array", "items": {"type": "string", "enum": ["walk", "stargaze"]}, "description": "What to plan for. Default both."},
    "min_duration_h": {"type": "number", "minimum": 0.5, "maximum": 24, "description": "Only walks whose estimated time is at least this many hours."},
    "max_duration_h": {"type": "number", "minimum": 0.5, "maximum": 24, "description": "Only walks whose estimated time is at most this many hours."},
    "min_length_km": {"type": "number", "minimum": 0.5, "maximum": 100, "description": "Only walks at least this long, in km."},
    "max_length_km": {"type": "number", "minimum": 0.5, "maximum": 100, "description": "Only walks at most this long, in km."},
    "max_ascent_m": {"type": "integer", "minimum": 1, "maximum": 3000, "description": "Only walks with at most this much ascent, in metres."},
    "max_rain_mm_h": {"type": "number", "minimum": 0, "maximum": 10, "description": "Walks: an hour counts as good only with at most this much rain, in mm. 0 means dry; the default 2 allows light rain; higher accepts wetter hours."},
    "max_wind_kmh": {"type": "number", "minimum": 0, "maximum": 120, "description": "Walks: an hour counts as good only with mean wind at most this, in km/h, at the walk's start. Default 80."},
    "min_temp_c": {"type": "number", "minimum": -30, "maximum": 30, "description": "Walks: an hour counts as good only at or above this temperature, in degrees C. Default none."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3, "description": "Options per activity per day."}
  },
  "required": ["lat", "lon"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Plan a Scottish outdoor trip"),
	},
	{
		Name:  "check",
		Title: "Check a Scottish outdoor plan",
		Description: "Check whether a plan is on: a named walk on a date, or stargazing at a place on a night, or several at once (a walk then stargazing the same day). " +
			"Use this when someone already has a plan: is Ben Nevis on for Saturday, if not Saturday then when, does a walk in Glencoe then stargazing on Rannoch Moor work on Friday. " +
			"For each item it returns a verdict (go, marginal, no, or unknown beyond the hourly forecast, about two and a half days ahead) with a one-line why, " +
			"the timing (for a walk: its slot hour by hour with rain, wind, temperature, cloud, low cloud and fog, the window to start in, sunrise and sunset; for a night: the clear window at the best dark-sky site near the point and the moon), " +
			"the other forecast days that suit it, and better options nearby the same day (for a walk, walks of similar length). " +
			"Walks are found by walk_name (words from the Walkhighlands name, for example \"Ben Nevis\") or walk_id from plan. " +
			"max_rain_mm_h, max_wind_kmh and min_temp_c change what counts as a good walking hour, as in plan. " +
			"Walk weather is at the walk's start, not on summits. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "minItems": 1,
      "maxItems": 10,
      "items": {
        "type": "object",
        "properties": {
          "activity": {"type": "string", "enum": ["walk", "stargaze"]},
          "date": {"type": "string", "format": "date", "description": "The day (YYYY-MM-DD, UK date), from today up to 13 days ahead; for stargazing, the evening the night starts, so before 09:00 the night under way is yesterday's date."},
          "walk_name": {"type": "string", "maxLength": 100, "description": "For a walk: words from its Walkhighlands name. Accents optional."},
          "walk_id": {"type": "string", "description": "For a walk: its walk_id from plan, instead of walk_name."},
          "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "For stargazing: latitude in decimal degrees."},
          "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "For stargazing: longitude in decimal degrees."}
        },
        "required": ["activity", "date"],
        "additionalProperties": false
      }
    },
    "max_rain_mm_h": {"type": "number", "minimum": 0, "maximum": 10, "description": "Walks: an hour counts as good only with at most this much rain, in mm. 0 means dry; the default 2 allows light rain; higher accepts wetter hours."},
    "max_wind_kmh": {"type": "number", "minimum": 0, "maximum": 120, "description": "Walks: an hour counts as good only with mean wind at most this, in km/h, at the walk's start. Default 80."},
    "min_temp_c": {"type": "number", "minimum": -30, "maximum": 30, "description": "Walks: an hour counts as good only at or above this temperature, in degrees C. Default none."}
  },
  "required": ["items"],
  "additionalProperties": false
}`),
		Annotations: readOnly("Check a Scottish outdoor plan"),
	},
}

// call decodes arguments and runs the tool.
func call(svc *connector.Service, name string, args json.RawMessage) (*connector.Envelope, error) {
	if len(args) == 0 || string(args) == "null" {
		args = json.RawMessage(`{}`)
	}
	switch name {
	case "light":
		var p connector.LightParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.Light(p)
	case "aurora":
		var p connector.AuroraParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.Aurora(p)
	case "plan":
		var p connector.PlanParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.Plan(p)
	case "check":
		var p connector.CheckParams
		if err := strictDecode(args, &p); err != nil {
			return nil, err
		}
		return svc.Check(p)
	}
	return nil, errUnknownTool
}
