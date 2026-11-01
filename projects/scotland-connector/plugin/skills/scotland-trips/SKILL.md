---
name: scotland-trips
description: Plan or check walks, stargazing and photo light in Scotland with the scotland MCP tools. Use for trip itineraries, named walks, sunrise, golden or blue light, twilight and Milky Way windows anywhere in Scotland.
---

# Scottish walks and stargazing

Use `plan` for itineraries, `check` for a specific walk or night, and `light`
for astronomical events and photo windows. Use `aurora` only for its separate
near-term heuristic when available; unsupported inputs remain unknown.
Read each `check` item's verdict (`go`, `marginal`, `no` or `unknown`)
and one-line `why` in UK
local time; `plan` lists only `go` and `marginal` options, and a day's
`walk_note` or `stargazing_note` says why it has none, except for a run beyond
the hourly horizon. Read that reason once in `datasets.<activity>.notes`;
daily notes may be absent or contain only moon advice. Lead with the verdict
and the why; they are written to be repeated.

Read `datasets.walk.hourly_until` and `datasets.stargaze.hourly_until`
independently. Quote each dataset's `notes` once for its inclusive `from`/`to`
run beyond the hourly horizon. Preserve per-day partial coverage and moon
advice. Treat null horizons and missing data as unknown, never forecast weather.

## plan or check

- **`plan`** for open questions: "where should I walk this weekend near
  Aviemore", "what can I do each day on Skye", "best night for stars near
  Inverness this week". It needs `lat` and `lon` for the place the person
  means, in decimal degrees; work them out yourself from the place name.
- **`check`** when the person already has a plan: "is Ben Nevis on for
  Saturday", "if not Saturday, when", "a walk in Glencoe then stars on
  Rannoch Moor on Friday". Name walks with `walk_name` (words from the
  Walkhighlands name) or a `walk_id` from an earlier plan. Several items can
  go in one call.

Dates are UK calendar dates (`YYYY-MM-DD`). A stargazing date is the evening
the night starts. A plan covers today and the next two days unless you pass
`from` and `to`: up to 7 days, starting up to 13 days ahead. For "this
weekend", pass that Saturday and Sunday.

## Photo light and discovery

Pass `lat`, `lon` and optional `from`/`to` to `light` for up to seven
Europe/London dates. Select windows with `kinds`, for example
`["golden", "blue"]` for photography. Accept `golden`, `blue`, `civil`,
`nautical`, `astronomical`, `darkness` and `milky_way`; omitted or empty arrays
return all. Ignore order and duplicates; avoid blank or unknown entries.
Read filter `civil` as window kind `civil_twilight`, and `darkness` as
`astronomical_darkness`. Keep the daily sun/moon events too.

Present geometry without inventing weather. Read `cloud_why` once per day;
an omitted window `cloud` means unknown/none, zero samples and supported
seconds. Preserve a present window's positive support. Treat `nights` as
separate night suitability, never an interval cloud forecast. Light has no
top-level verdict and no visibility guarantee.

Use prompts `plan_trip`, `check_walk` and `photo_light` for tool-call guidance;
inspect their string arguments with `prompts/list`. Read
`scotland://docs/method` for method details and `scotland://docs/openapi` for
the contract through `resources/read`. Match structured answers against each
tool's `outputSchema`, derived from those OpenAPI components.

## Match the weather to the person

By default a good walking hour has at most 2 mm of rain and wind at most
80 km/h, so a `go` can include drizzle. Translate what the person says into
limits on both tools:

| They say                     | Pass                      |
| ---------------------------- | ------------------------- |
| "no rain at all", "dry"      | `max_rain_mm_h: 0`        |
| "a bit of drizzle is fine"   | nothing (the default)     |
| "I don't mind getting wet"   | `max_rain_mm_h: 4`        |
| "not too windy"              | `max_wind_kmh: 40`        |
| "not cold", "above freezing" | `min_temp_c: 0` or higher |

Size and effort go on `plan` as `max_duration_h`, `min_duration_h`,
`max_length_km`, `min_length_km` and `max_ascent_m`.

## Read the answer honestly

- **`unknown`** means the hourly forecast, about two and a half days ahead,
  does not reach that day yet. Say so and offer to check again nearer the
  time; for nights, quote the clear-sky odds it gives.
- **`marginal`** names its catch in the `why` (a wet hour mid-walk, wind,
  needing to start or finish in the dark). Pass the catch on.
- Walk weather is forecast at the walk's start; the summit is usually colder
  and windier. For hill days, suggest the Mountain Weather Information
  Service as well.
- Link each walk's `url` for the route; the connector does not carry route
  descriptions.
- Credit the sources listed in the answer's `meta` when you summarise it.
