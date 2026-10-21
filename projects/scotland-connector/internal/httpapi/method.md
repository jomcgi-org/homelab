# Method

Two questions, two calls. **plan** answers "what should I do near here over
the next few days": for each day, the sun and moon at the point, the walks
that fit the weather and the night's best stargazing. **check** answers "is
my plan on": for a named walk on a date, or stargazing at a point on a night,
a verdict, the timing, the other days that suit it and better options nearby.

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

A **good hour** is a daylight hour (between sunrise and sunset at the start)
with at most 2 mm of rain and wind at most 80 km/h.

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
the slot.

**In a plan**, walks whose start is within `radius_km` of the point are
assessed for each day; go comes before marginal, then the longest slot, then
the nearest. Walks that do not fit are left out, and the day's `walk_note`
says why when none fit.

**In a check**, a walk is found by `walk_id` or by `walk_name`, which matches
Walkhighlands names ignoring case, accents and apostrophes: every word given
must start a word of the name. A name that starts with the whole query ranks
first, then shorter names. Other matches are listed in `also_matched`. When
the walk is not a go, `nearby_walks` offers up to two that fit better the
same day: of similar length (half to one and a half times its time),
starting within 20 km, nearest first.

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
