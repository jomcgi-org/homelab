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
		Name:  "plan",
		Title: "Plan a Scottish outdoor trip",
		Description: "Find what to do near a place in Scotland over the next few days: walks that fit the weather and where the night sky will be dark and clear. " +
			"Use this for open questions: where to walk this weekend, what to do each day of a trip to Skye, the best night this week for stars near Inverness. " +
			"For each day it returns sunrise, sunset and the moon, then the best walks and stargazing sites that suit it, each with a verdict (go or marginal) and a one-line why; " +
			"when none suit, a note says why instead, including when the hourly forecast, about two and a half days ahead, does not reach that far yet. " +
			"Walks are Walkhighlands routes that fit a dry, calm daylight slot for their estimated time, with the slot, a start window and the url for the route. " +
			"Stargazing sites are road-accessible spots in the darkest light-pollution zones, judged on clear dark hours and the moon. " +
			"Walk weather is at the walk's start, not on summits. Give lat and lon in decimal degrees for the place the person means. Scotland only.",
		InputSchema: json.RawMessage(`{
  "type": "object",
  "properties": {
    "lat": {"type": "number", "minimum": 54.5, "maximum": 61.0, "description": "Latitude in decimal degrees."},
    "lon": {"type": "number", "minimum": -9.0, "maximum": -0.5, "description": "Longitude in decimal degrees."},
    "radius_km": {"type": "number", "minimum": 1, "maximum": 100, "default": 30, "description": "How far from the point to look, in km."},
    "from": {"type": "string", "format": "date", "description": "First day (YYYY-MM-DD, UK date), from today up to 13 days ahead. A day's stargazing is the night of its evening; before 09:00 the night under way is yesterday's, which from may also be. Default today, or that night's date when stargazing is planned."},
    "to": {"type": "string", "format": "date", "description": "Last day (YYYY-MM-DD); a plan covers at most 7 days. Default two days after from."},
    "activities": {"type": "array", "items": {"type": "string", "enum": ["walk", "stargaze"]}, "description": "What to plan for. Default both."},
    "max_duration_h": {"type": "number", "minimum": 0.5, "maximum": 24, "description": "Only walks whose estimated time is at most this many hours."},
    "max_ascent_m": {"type": "integer", "minimum": 1, "maximum": 3000, "description": "Only walks with at most this much ascent, in metres."},
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
			"the timing (for a walk: its slot, the window to start in, sunrise and sunset; for a night: the clear window at the best dark-sky site near the point and the moon), " +
			"the other forecast days that suit it, and better options nearby the same day (for a walk, walks of similar length). " +
			"Walks are found by walk_name (words from the Walkhighlands name, for example \"Ben Nevis\") or walk_id from plan. " +
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
    }
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
