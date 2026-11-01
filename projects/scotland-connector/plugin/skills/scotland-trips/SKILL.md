---
name: scotland-trips
description: Plan or check walks and stargazing in Scotland with the scotland MCP tools. Use when someone asks where to walk, whether a named walk or night is on, which day suits a hill, or where to see the stars anywhere in Scotland.
---

# Scottish walks and stargazing

The `scotland` MCP server answers with `plan`, `check` and `light`, and
`aurora` when it is listed. `check` answers each item with a verdict (`go`,
`marginal`, `no` or `unknown`) and a one-line `why` in UK local time; `plan`
lists only `go` and `marginal` options, and a day's `walk_note` or
`stargazing_note` says why it has none. Lead with the verdict and the why;
they are written to be repeated. `light` has no verdict: it gives sunrise,
sunset, twilight, golden and blue light, the moon and Milky Way windows for
photographers and stargazers.

## plan or check

- **`plan`** for open questions: "where should I walk this weekend near
  Aviemore", "what can I do each day on Skye", "best night for stars near
  Inverness this week". It needs `lat` and `lon` for the place the person
  means, in decimal degrees; work them out yourself from the place name.
- **`check`** when the person already has a plan: "is Ben Nevis on for
  Saturday", "if not Saturday, when", "a walk in Glencoe then stars on
  Rannoch Moor on Friday". Name walks with `walk_name` (words from the
  Walkhighlands name) or a `walk_id` from an earlier plan. Several items can
  go in one call. When `match_note` is set, the name was ambiguous: say which
  walk was checked and offer the `also_matched` alternatives.

Dates are UK calendar dates (`YYYY-MM-DD`). A stargazing date is the evening
the night starts. A plan covers today and the next two days unless you pass
`from` and `to`: up to 7 days, starting up to 13 days ahead. For "this
weekend", pass that Saturday and Sunday.

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
  needing to start or finish in the dark). Pass the catch on; in winter a
  long hill day is often marginal only because it needs a head torch.
- **`clear_sky_odds`** is a site's historical share of clear dark hours in
  that calendar month. Quote it as climatology; the verdict covers tonight.
- Walk weather is forecast at the walk's start; the summit is usually colder
  and windier. For hill days, suggest the Mountain Weather Information
  Service as well.
- Link each walk's `url` for the route; the connector does not carry route
  descriptions.
- Credit the sources listed in the answer's `meta` when you summarise it.
