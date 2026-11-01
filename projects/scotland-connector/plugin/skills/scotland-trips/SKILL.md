---
name: scotland-trips
description: Plan or check walks and stargazing in Scotland, or compare stargazing seasons beyond the forecast using historical climatology and local astronomy.
---

# Scottish walks and stargazing

The `scotland` MCP server includes trip planning and seasonal outlook. `check` answers each item with a
verdict (`go`, `marginal`, `no` or `unknown`) and a one-line `why` in UK
local time; `plan` lists only `go` and `marginal` options, and a day's
`walk_note` or `stargazing_note` says why it has none. Lead with the verdict
and the why; they are written to be repeated.

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

## outlook beyond the forecast

Use `outlook` for "best month for stars on Skye" or "moonless dates next
March". Pass a Scottish `lat`/`lon`, optional `radius_km` (1..100, default
30), and inclusive Europe/London `from`/`to` dates in 2000..2100. It defaults
to today through the day before next year's anniversary, with a one-calendar-year
maximum; defaults stop at 2100-12-31. February 29's anniversary clamps to February 28 and is exclusive;
2028-02-29 through 2029-02-27 is valid. No forecast horizon caps apply.
At most 92 inclusive dates return daily moon phase, moonless windows and
climatology ranks. Longer spans explicitly omit daily detail while keeping
all monthly calculated totals.

Weather is always `unknown`. Say "historically" when quoting CERRA's
whole archive month-of-year totals. Its native three-hour cloud samples
were nearest-reindexed hourly; the snapshot does not identify archive years.
Never call these totals expected hours for one trip or a fresh probability.
Rate ranking needs at least 300 historical dark hours in each calendar month;
null means unsupported, including a summer month with too little darkness.
Present rate and absolute-hours rankings separately and name different
winning sites when they disagree. Daily ranks inherit their month's
climatology and do not distinguish one date's weather from another's.

Moonless windows and calculated hours describe the requested point, independent
of either historical winning site: sun below -12 degrees and topocentric
moon below -0.833 degrees, without terrain. UTC windows are clipped to local
civil dates, including DST. Phase is sampled at local noon, with 0=new,
.25=first quarter, .5=full and .75=last quarter. Missing history leaves ranks
unknown while astronomy can still be shown. Credit CERRA and the local
astronomical source from `meta.sources`. Use plan/check nearer the trip to
assess the available hourly weather.

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
