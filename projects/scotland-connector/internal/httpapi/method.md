# Method

Two questions, two calls. **plan** answers "what should I do near here over
the next few days": for each day, the sun and moon at the point, the walks
that fit the weather and the night's best stargazing. **check** answers "is
my plan on": for a named walk on a date, or stargazing at a point on a night,
a verdict, the timing, the other days that suit it and better options nearby.

## Outlook beyond the forecast

Use `outlook` or `GET /v1/outlook` for seasonal stargazing questions at a
Scottish point. Inputs are `lat`, `lon`, `radius_km` (1..100, default 30),
`from` and `to`. Dates are inclusive Europe/London civil dates in 2000..2100.
The default is today through the day before the next calendar-year
anniversary, capped at 2100-12-31. The inclusive end must precede that anniversary; February 29
clamps to February 28, so 2028-02-29 through 2029-02-27 is valid and through
2029-02-28 is refused. January through December can contain 366 dates in a
leap year. Year-crossing requests can have 13 month segments. Forecast's
seven-day and 13-day caps do not apply. Spans of at most 92 inclusive civil
dates include daily detail. Longer spans explicitly omit all daily detail
while still calculating monthly geometry for every requested date.

Weather verdict is always **unknown**, including the top historical rank.
Each month's `why` gives available CERRA figures and explains climatology.
`rate_winner` and `hours_winner` identify sites and coordinates, selected from
all nearby sites independently of forecast freshness. Rate uses clear-dark
hours divided by dark hours only with at least **300 historical dark hours**
in that calendar month. Insufficient support gives a null rate/rank. Hours
ranking remains separate; never combine them or hide different winners.
Tied site scores use distance then ID. Rankings compare returned months;
equal values share competition rank, with chronological ranking-list ties.
Daily ranks are explicitly climatological and inherit the month's rank.

Historical fields are pooled whole-month-of-year archive hours, never
expected hours for one requested month or a freshly measured probability.
The loaded snapshot carries no archive years or exposure dates. CERRA cloud
was sampled every three hours and nearest-reindexed hourly, with dark = sun
below -12 degrees, clear-dark also cloud below 10%. Hourly counts are not
independent cloud observations. Partial requested months do not prorate
historical totals. Absent or invalid climatology leaves winners/ranks null.

`calculated_dark_hours`, `calculated_moonless_dark_hours` and daily windows
describe the rounded requested point, independent of winning sites. Local
astronomy intersects nautical darkness (sun centre below -12 degrees) with
topocentric moon centre below -0.833 degrees, an approximate refraction/disc
allowance. Windows are UTC RFC3339, clipped to Europe/London midnight
boundaries including 23/25-hour DST dates. Empty windows mean no qualifying
interval. Phase and illumination are sampled at local noon, with a recorded
reference time; new, first quarter, full and last quarter are 0, .25, .5, .75.
No terrain or clear-weather guarantee is implied. Monthly calculated hours
sum every requested date even with omitted daily detail or absent history.
One-second numerical solver resolution does not establish ephemeris accuracy.
CERRA and the local astronomical source are credited in `meta.sources`.
No upstream calls or new datasets are involved. Cache headers and ETags
follow plan; `forecast_fetched_at` is null for Outlook.

## Verdicts

Every walk and night leads with a verdict and a one-line `why` in UK local
time.

- **go**: it fits.
- **marginal**: it fits with a catch the `why` names.
- **unknown**: the hourly forecast does not reach far enough to say yet.
- **no**: it does not fit.

MET Norway's forecast is hourly for about two and a half days, and the
connector counts 48 hours from each fetch as hourly. Days further out, up to
13 days ahead, come back **unknown**; for nights the `why` quotes how often
the site has been clear in that month. A plan lists only go and marginal
options; a day with none says why in `walk_note` or `stargazing_note`.

## Walks

**Walks** are the Walkhighlands catalogue: name, start point, length, ascent
and estimated time, with a link to the walk's page. The route, maps and
descriptions stay on Walkhighlands; follow the link for them.

**Forecast** is MET Norway's Locationforecast at each walk's start point,
fetched every two hours. It is not a summit forecast: conditions on high
ground are usually colder and windier. For hill days also check the Mountain
Weather Information Service.

A **good hour** is a complete daylight hour (starting at or after sunrise and ending by sunset at the walk's start)
with at most 2 mm of rain and wind at most 80 km/h. 2 mm in an hour is light
rain, so by default a good hour may be drizzly.

**Weather limits.** `max_rain_mm_h`, `max_wind_kmh` and `min_temp_c`, on
plan and check, change what counts as a good hour to what the person will put
up with: `max_rain_mm_h=0` asks for dry hours only, and `max_rain_mm_h=4`
accepts wetter ones. Temperature has no default limit. The limits apply
before the slot is found, so they change the verdict, the start window, the
other days and the walks offered nearby. Loosening needs forecast data that
carries every daylight hour; on data that holds the default good hours only,
such a limit is refused as not available yet rather than silently ignored.
There is no limit on gusts or the chance of rain: MET Norway forecasts them
only within its Nordic model, which in Scotland covers Shetland alone. Each
limit rounds to the
data's 0.1 in its strict direction (a maximum down, a minimum up), so
rounding never admits an hour the caller ruled out. Under limits the hours
beside a slot are often daylight the person ruled out, so a long walk's
dark credit counts only on a side of the slot that meets real darkness
(before sunrise or after sunset at the start): with neither, the walk does
not fit, and with only the far side, it starts with the slot and finishes in
the dark.

The **slot** is the day's longest run of good hours, with one bad hour
tolerated inside it. A walk **fits** when the slot holds at least 80% of its
estimated time; walks over seven hours get two hours of credit for starting
or finishing in the dark. Only hours that have not begun count, so at 11:20
today's slot starts at 12:00 at the earliest, and the start window never lies
in the past.

- **go** when it fits in daylight, with no bad hour inside the slot and wind
  at most 50 km/h.
- **marginal** when it fits only with the dark credit, with a bad hour
  mid-walk, or in wind over 50 km/h.
- **no** when the slot is too short, or there is no good hour at all.
- **unknown** when the hourly forecast ends before the day's daylight does
  and no slot long enough has appeared yet.

`start_from` and `start_by` bound the start time that keeps the walk inside
the slot. The slot carries its highest wind, total rain, wettest hour
(`max_rain_mm_h`), `wet_hours` (good hours with at least 0.1 mm of rain) and
temperature range across its good hours, so an agent can tell a dry, calm
slot from a drizzly, breezy one. A check also returns the checked walk's slot
hour by hour (`hours`): rain, wind, temperature, cloud, low cloud and fog,
each where the forecast gives it. The one bad hour a slot may hold is
`good: false` with `fails` naming what it failed (`rain`, `wind`, `cold`); on
data that holds good hours only, a bad hour that failed the defaults has no
weather.

**In a plan**, walks whose start is within `radius_km` of the point, and
that pass the filters on estimated time (`min_duration_h`,
`max_duration_h`), length (`min_length_km`, `max_length_km`) and ascent
(`max_ascent_m`), are assessed for each day; go comes before marginal, then the longest slot, then
the nearest. Walks that do not fit are left out, and the day's `walk_note`
says why when none fit.

**In a check**, a walk is found by `walk_id` or by `walk_name`, which matches
Walkhighlands names ignoring case, accents and apostrophes: every word given
must start a word of the name. A name that starts with the whole query ranks
first, then shorter names. Other matches are listed in `also_matched`. When
the walk is not a go, `nearby_walks` offers up to two that fit better the
same day: of similar length (half to one and a half times its time),
starting within 20 km, nearest first.

## Light windows

`light` (REST `GET /v1/light`) computes light at the supplied coordinates,
even when no forecast snapshot is available. This is staged repository
delivery; operational acceptance remains on #7367.

`from` and `to` are inclusive Europe/London civil dates. Today through two
days later is the default, with at most seven dates and nothing later than
13 days ahead. Coordinates round to 0.01 degrees. `radius_km` has plan's
1 to 100 km bounds and 30 km default; the effective forecast-site reach is
at most 30 km. The nearest site wins, with ID breaking exact distance ties.
Astronomy stays at the requested point; the returned `site` identifies where
the weather samples came from, with its fetch time, horizon and truncation.

All event times are UTC RFC3339 seconds. Each date owns local midnight to
the next midnight, so DST dates can have 23 or 25 hours. Null means the
crossing does not occur on that date. Absent windows are omitted, with an
empty array when none exist. Intervals include `from` and exclude `until`,
are clipped at midnight and sorted by start then kind. The same night can
appear as two civil-date pieces. Each window carries the existing
noon-to-noon night label at its midpoint.

Sunrise/set use centre altitude -0.833 degrees. Civil, nautical and
astronomical dawn/dusk cross -6, -12 and -18 degrees, with twilight windows
between adjacent thresholds (civil ends at -0.833). Golden light is sun
-4 to +6 degrees; blue light is -6 to -4. Moonrise/set use topocentric
centre altitude -0.833 degrees as an approximate refraction and disc-radius
allowance. Moon phase and lit fraction are sampled at local noon,
`reference_at`: new 0, first quarter .25, full .5 and last quarter .75.
Direction is waxing before the unrounded phase .5 and waning after it.

Milky Way windows require fixed galactic-centre RA 17h45m, declination
-29 degrees above the geometric horizon and sun below -12 degrees
(nautical darkness, matching stars). July can retain astronomical twilight
throughout the night. Twilight and moonlight still limit faint details;
these are geometry windows with no guarantee of a useful view.
Astronomical darkness below -18 degrees is returned separately. Computation
uses the existing truncated solar/lunar series and linear sidereal time,
without terrain or core refraction. Numerical events resolve to about a
second; the simplified astronomical model has lower physical accuracy.

Every window has `cloud`, scoped to hourly sample support at the nearby
site. The stars snapshot retains only already-clear (cloud below 10%)
twilight-dark (sun below -10 degrees) samples. Their one-hour buckets are
intersected with the window and requested-point sun below -10.
`supported_seconds` measures the union of those intersections. `samples`
counts matching rows. `clear` requires the whole window to have sample
support; otherwise `unknown` has `partial` or `none` coverage. These are
sampled forecasts, with no claim of continuously clear conditions at the
point. Golden/blue cloud is unknown because those observations are absent.
No missing row proves cloud. No new forecast feed is queried.

Missing, future-dated or expired site fetches are unknown. Hours before the
as-of hour or the site's fetch hour are dropped; complete hourly buckets
must end by the earlier site/dataset 48-hour horizon. Truncated lists retain
their positive samples but cannot establish anything about omitted hours.
`nights` separately reuses `AssessNight` for suitability, including moonlight
and coverage. A whole-night verdict is never an interval cloud forecast.
Moon advice text is omitted there because `days` already carries the geometry.

Light has plan's cache policy: browsers revalidate after 60 seconds and the
Cloudflare-only header gives the edge 600 seconds, with the same bounded
stale directives. The ETag includes snapshot version, as-of hour, exact
freshness state and canonical query. Forecast updates can change the cloud
part. Headers and fixture tests establish the repository contract; actual
edge hits, query isolation and published cloud coverage remain live checks.

## Stargazing

A **clear-dark hour** is a forecast hour at a site where cloud cover is below
10% and the sun is more than 12 degrees below the horizon. A **clear-twilight
hour** relaxes the sun to 10 degrees below. For about seven weeks around
midsummer Scotland gets no darkness past -12 degrees, and twilight hours are
then the darkest available.

**Sites** are a 2 km grid over Scottish land, kept where they are within 2 km
of a drivable road (OpenStreetMap) and fall in the three darkest
light-pollution zones (pristine, excellent, rural).

**Forecast** cloud cover is MET Norway's Locationforecast for each site,
fetched every three hours.

**Nights** run from local noon to local noon (Europe/London) and are named by
the date of their evening: a plan day's stargazing, and a check's `date`, are
the night that starts that evening. Until 09:00 the night under way is
yesterday's date, and plan and check accept it.

**The moon.** A clear-dark hour is **moonless** when the moon is below the
horizon or under 10% lit. Positions come from truncated lunar and solar
series after Meeus (Astronomical Algorithms), within about a third of a
degree of a full ephemeris. `moon_illumination` is the lit fraction at local
midnight after the evening.

**Dark score** weighs a night's clear-dark hours by the moon: a moonless hour
counts 1, a moonlit one 1 minus the moon's lit fraction.

- **go** at a dark score of 3 or more.
- **marginal** when it is clear but short of that: only twilight-dark, too
  few clear-dark hours, or a bright moon.
- **no** when it is cloudy all night.
- **unknown** when the hourly forecast ends before the night does and what is
  forecast is not already a go.

The stars snapshot lists at most 25 clear hours per site, earliest first. A
night past the end of a capped site's list is **unknown** unless it is
already a go: its later hours were left out of the snapshot, not forecast
cloudy.

`clear_from` and `clear_until` bound the night's clear-dark hours (its
clear-twilight hours when none is dark), which may have gaps.

**In a plan**, sites within `radius_km` are ranked by verdict, then dark
score, then distance, and kept at least 5 km apart. **In a check**, the
answer is the best site within 10 km of the point, widening to 30 km with a
`site_note` when none is that close.

**Clear-sky odds** are the share of dark hours that were clear in that
calendar month over the Copernicus CERRA reanalysis record.

## Sun

Sunrise and sunset use the NOAA solar equations at the point, with the
standard 50 arcminutes for refraction and the sun's radius.

## Places

Pass the latitude and longitude of the place the person means; they are
rounded to 0.01 degrees (about 1 km), and anything outside Scotland is
refused. Distances are great-circle approximations, accurate to well under
0.1 km within the 100 km radius cap.

## Freshness

`meta.forecast_fetched_at` is the older of the newest MET Norway fetches
behind the answer; `meta.as_of` is the hour it is computed for. plan answers
are cached at the edge for a few minutes and served stale if the origin is
unavailable. check answers are POSTs and are not cached.
## Near-term aurora (staged)

`aurora` (MCP) and `GET /v1/aurora?lat=...&lon=...&at=...` share one
snapshot-only engine. Supply coordinates and an RFC3339 timestamp with timezone.
The assessment returns `go`, `marginal`, `no` or `unknown`, with per-input
evidence and source times. Activation defaults off. Disabled and unloaded
snapshots return `unknown`; plan, check and readiness keep their existing rules.

NOAA OVATION supplies an observation time, a forecast time and a global
longitude/latitude intensity grid. Its usual 30 to 90 minute lead varies with
solar-wind travel time. The current-Kp fallback can have no lead. The JSON
does not identify that forcing mode or supply a valid-until timestamp. This
version supports the exact forecast instant only, with requests between now
and two hours ahead. Callers can read `noaa.forecast_at` from an unknown answer
and ask for that instant. An equal observation/forecast timestamp is reported
as `no_lead`; this alone does not identify the fallback mode.

Application freshness limits are separate: NOAA collection under 20 minutes,
observation under two hours, MET collection under four hours and MET issue
under six hours. Exactly at an expiry limit is unknown. Future input times
are unknown. Refreshing or retaining a cached object never extends support.
Both HTTP cache headers are `no-store`, including conditional requests.

The nearest NOAA cell must be within 80 km. A supported site must be within
1 km, with unrounded coordinates. Weather uses MET's unfiltered instant cloud
rows: an exact row, or the larger cloud fraction from two bracketing rows no
more than an hour apart. It never bridges a missing hour or extrapolates a
capped row. Darkness uses shared solar-coordinate calculations at the requested
site/time, with the stars policy's -10 and -12 degree thresholds.

Every input must be supported before any assessment other than unknown.
Sun at or above -10 degrees, cloud at least 75%, sampled northern terrain at
least 10 degrees, or NOAA intensity below 10 gives `no`. Sun below -12,
cloud at most 25%, terrain at most 3 degrees and intensity at least 30 gives
`go`; intermediate conditions are `marginal`. These are conservative heuristic
thresholds, not calibrated local sighting probabilities or travel advice.
Terrain covers 19 sampled bearings from northwest to northeast, to 20 km.
It excludes trees, buildings, distant terrain beyond that radius and access
conditions. Coarse raster sampling can miss narrow ridges. Null cells and
absent terrain remain unknown. Synthetic test terrain proves computation only.

Sources: [NOAA product and fallback description](https://www.spaceweather.gov/products/aurora-30-minute-forecast),
[MET instant-variable contract](https://api.met.no/doc/locationforecast/datamodel),
[GMTED2010 metadata and terms](https://topotools.cr.usgs.gov/gmted_viewer/gmted2010_fgdc_metadata.html).
