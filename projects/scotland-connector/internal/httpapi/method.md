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
refreshed hourly. Hours already past are dropped at the top of each hour.

**Nights** run from local noon to local noon (Europe/London) and are named by
the date of their evening. "Tonight" is the night in progress until 09:00
local and the coming evening's after that.

**Ranking** is clear-dark hours over the requested nights, then clear-twilight
hours, then darker light-pollution zone, then distance.

**Clear-sky odds** are the share of dark hours that were clear in that
calendar month over the Copernicus CERRA reanalysis record.

**Distances** are great-circle approximations, accurate to well under 0.1 km
within the 150 km radius cap.

## Places

Place names resolve against a small curated gazetteer of Scottish towns,
villages and dark-sky places, with Gaelic names where in common use.
Coordinates are approximate centroids. Pass coordinates directly when you
have them.

## Freshness

`meta.forecast_fetched_at` is the newest MET Norway fetch in the data;
`meta.as_of` is the hour the answer is computed for. Answers are cached at the
edge for a few minutes and served stale if the origin is unavailable.
