# Method

## Dark-sky windows

A **clear-dark hour** is a forecast hour at a site where cloud cover is below
10% and the sun is more than 12 degrees below the horizon. A **clear-twilight
hour** relaxes the sun to 10 degrees below. For about seven weeks around
midsummer Scotland gets no darkness past -12 degrees, and twilight hours are
then the darkest available.

**Sites** are a 2 km grid over Scottish land, kept where they are within 2 km
of a drivable road (OpenStreetMap) and fall in the three darkest
light-pollution zones (pristine, excellent, rural).

**Forecast** cloud cover is MET Norway's Locationforecast for each site,
fetched every three hours. Hours already past are dropped at the top of each hour.

**Nights** run from local noon to local noon (Europe/London) and are named by
the date of their evening. "Tonight" is the night in progress until 09:00
local and the coming evening's after that.

**Ranking** is clear-dark hours over the requested nights, then clear-twilight
hours, then darker light-pollution zone, then distance.

**The moon.** Each night carries the moon's lit fraction at local midnight,
and each clear hour (with `detail=hours`) the moon's altitude at the site. A
clear-dark hour is **moonless** when the moon is below the horizon or under
10% lit. Positions come from truncated lunar and solar series after Meeus
(Astronomical Algorithms), within about half a degree of a full ephemeris.

**Best night** is the night with the most moonless clear-dark hours, then the
most clear-dark hours, then the most clear-twilight hours, earliest first.
`clear_from` and `clear_until` bound a night's clear hours, which may have
gaps.

**Clear-sky odds** are the share of dark hours that were clear in that
calendar month over the Copernicus CERRA reanalysis record.

**Distances** are great-circle approximations, accurate to well under 0.1 km
within the 150 km radius cap.

## Walk windows

**Walks** are the Walkhighlands catalogue: name, start point, length, ascent
and estimated time, with a link to the walk's page. The route, maps and
descriptions stay on Walkhighlands; follow the link for them.

**Forecast** is MET Norway's Locationforecast at each walk's start point,
fetched every two hours. It is not a summit forecast: conditions on high
ground are usually colder and windier.

A **good hour** is a daylight hour (between sunrise and sunset at the start)
with at most 2 mm of rain and wind at most 80 km/h. Only good hours are
listed; within the forecast, a missing daylight hour failed one of those
limits.

**Coverage.** MET Norway's forecast is hourly for about two and a half days,
so a query covers at most three days. Each day says whether the hourly
forecast covers its daylight in full, in part, or not at all, and `doable`
is null when the forecast does not reach far enough to decide.

A day is **doable** when its good hours hold a run of at least 80% of the
walk's estimated time, with one bad hour tolerated inside the run. Walks over
seven hours get two hours of credit for starting or finishing in the dark.
Days are UK calendar days, starting today.

**Ranking** is doable days, then the earliest doable day, then good hours,
then distance from the requested point to the walk's start.

**By name.** `walk_name` matches Walkhighlands walk names, ignoring case,
accents and apostrophes: every word given must start a word of the name. A
name that starts with the whole query ranks first. Without `lat` and `lon`
every walk is searched.

**Best** is a walk's earliest doable day, with its slot and the highest wind,
total rain and temperature range across the slot's good hours. `date` asks
about one day up to 13 days ahead; past the hourly forecast that day comes
back with `forecast_coverage` `none`.

## Detail

Answers lead with the best night or slot and leave out hourly arrays.
`detail=hours` adds every clear hour (dark skies) or good hour (walks).

## Places

Queries take coordinates. Pass the latitude and longitude of the place the
person means; they are rounded to 0.01 degrees (about 1 km), and anything
outside Scotland is refused.

## Freshness

`meta.forecast_fetched_at` is the newest MET Norway fetch in the data;
`meta.as_of` is the hour the answer is computed for. Answers are cached at the
edge for a few minutes and served stale if the origin is unavailable.
