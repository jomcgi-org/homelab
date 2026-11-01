// Package walks holds the walk forecast snapshot (walks.json, written by the
// monolith's hikes-refresh-forecasts job) and answers "which walks near here
// have a long enough dry, calm daylight slot on which days".
//
// The day rule is the hikes app's (projects/monolith/hikes/router.py
// _doable_days): a day is doable when its good hours hold a run of at least
// 80% of the walk's estimated duration, tolerating one bad hour inside the
// run, with two hours of dark-shoulder credit for walks over seven hours.
package walks

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
	_ "time/tzdata" // the image ships no zoneinfo; days are UK calendar dates

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
)

// Snapshot shapes this package reads. Version 1 holds the good hours only,
// as [unix, temp_c, precip_mm, wind_kmh, cloud_pct]. Version 2 holds every
// daylight hour of the hourly forecast with two more fields, fog_pct and
// low_cloud_pct, either null when the forecast leaves it out.
const (
	SchemaV1 = 1
	SchemaV2 = 2
)

// Doable-day rule, mirrored from the hikes router.
const (
	durationGoodHourFraction = 0.8
	longWalkHours            = 7.0
	darkShoulderHours        = 2
	requirementEpsilon       = 1e-9
)

const kmPerDegLat = 111.195

// minHourlyHorizon is how far past the fetch MET Norway's hourly forecast is
// always taken to reach. Locationforecast is hourly for about two and a half
// days, then 6-hourly, and the hikes job keeps hourly steps only; 48 h stays
// under that for any model run the fetch could have seen.
const minHourlyHorizon = 48 * time.Hour

// Daylight bounds for coverage, in UK local time: no Scottish sunrise is
// earlier than 04:00 or sunset later than 23:00.
const (
	daylightStartHour = 4
	daylightEndHour   = 23
)

var london = mustLoad("Europe/London")

func mustLoad(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}

// LocalDay is the UK civil date of t as days since 1970-01-01.
func LocalDay(t time.Time) int32 {
	y, m, d := t.In(london).Date()
	return int32(time.Date(y, m, d, 0, 0, 0, 0, time.UTC).Unix() / 86400)
}

// Hour is one daylight hour at the walk's start point. In a version 1
// snapshot only good hours are present; in version 2 every daylight hour is.
// The version 2 fields are NaN when unknown.
type Hour struct {
	Unix        int64
	Day         int32
	TempC       float32
	PrecipMM    float32
	WindKmh     float32
	CloudPct    float32
	FogPct      float32
	LowCloudPct float32
}

var nan = float32(math.NaN())

// Known reports whether v, one of an hour's version 2 fields, was forecast.
func Known(v float32) bool { return v == v }

// Walk is one WalkHighlands walk with its viable hours in time order.
type Walk struct {
	ForecastFetchedAt time.Time
	ID                string
	Name              string
	URL               string
	Lat, Lon          float64
	LengthKm          float64
	AscentM           int
	DurationH         float64
	Hours             []Hour
	// good is Hours under DefaultTest, worked out once: most questions use
	// the default test, and a plan asks it of every walk on every day.
	good       []Hour
	cosLat     float64
	required   float64
	shoulderHr int
	nameTokens []string
	nameKey    string
}

// Dataset is one parsed snapshot.
type Dataset struct {
	Walks     []Walk
	FetchedAt time.Time
	Version   string
	// HourlyUntil is how far the hourly forecast is known to reach: the later
	// of FetchedAt plus minHourlyHorizon and the end of the latest hour in the
	// snapshot. Both are lower bounds, so a daylight hour before it that is
	// missing failed the default test (version 1) or was never in the data;
	// hours after it were never forecast.
	HourlyUntil time.Time
	// Skipped counts snapshot rows left out for missing fields, and
	// SkippedHours hour rows left out for a missing time.
	Skipped      int
	SkippedHours int
	// Complete reports a version 2 snapshot: every daylight hour is present,
	// so limits can loosen the default test and a bad hour's weather is
	// known.
	Complete bool
	// fullThrough and noneFrom index CoverageOf: days up to fullThrough are
	// full, days from noneFrom have none, and the rest are partial. A plan
	// asks for every walk on every day, so the answer is worked out once.
	fullThrough, noneFrom int32
	indexed               bool
}

type doc struct {
	SchemaVersion int     `json:"schema_version"`
	FetchedAt     *string `json:"fetched_at"`
	Walks         []struct {
		ForecastFetchedAt *string      `json:"forecast_fetched_at"`
		ID                string       `json:"id"`
		Name              string       `json:"name"`
		URL               string       `json:"url"`
		Lat               float64      `json:"lat"`
		Lon               float64      `json:"lon"`
		DistanceKm        float64      `json:"distance_km"`
		AscentM           int          `json:"ascent_m"`
		DurationH         float64      `json:"duration_h"`
		Hours             [][]*float64 `json:"hours"`
	} `json:"walks"`
}

// Parse reads walks.json. version identifies the snapshot (its ETag).
func Parse(body []byte, version string) (*Dataset, error) {
	var d doc
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("walks: parse: %w", err)
	}
	width := 0
	switch d.SchemaVersion {
	case SchemaV1:
		width = 5
	case SchemaV2:
		width = 7
	default:
		return nil, fmt.Errorf("walks: schema_version %d, want %d or %d", d.SchemaVersion, SchemaV1, SchemaV2)
	}
	if len(d.Walks) == 0 {
		return nil, fmt.Errorf("walks: snapshot has no walks")
	}
	ds := &Dataset{Version: version, Walks: make([]Walk, 0, len(d.Walks)), Complete: d.SchemaVersion == SchemaV2}
	if d.FetchedAt != nil && *d.FetchedAt != "" {
		t, err := time.Parse(time.RFC3339, *d.FetchedAt)
		if err != nil {
			return nil, fmt.Errorf("walks: fetched_at %q: %w", *d.FetchedAt, err)
		}
		ds.FetchedAt = t.UTC()
	}
	var latest int64
	for _, w := range d.Walks {
		// One bad row must not take out every walk: skip it and count it.
		if w.ID == "" || w.Name == "" || w.URL == "" || !(w.DurationH > 0) {
			ds.Skipped++
			continue
		}
		out := Walk{
			ID: w.ID, Name: w.Name, URL: w.URL, Lat: w.Lat, Lon: w.Lon,
			LengthKm: w.DistanceKm, AscentM: w.AscentM, DurationH: w.DurationH,
			Hours:    make([]Hour, len(w.Hours)),
			cosLat:   math.Cos(w.Lat * math.Pi / 180),
			required: durationGoodHourFraction * w.DurationH,
			nameKey:  Normalize(w.Name),
		}
		out.nameTokens = strings.Fields(out.nameKey)
		if w.ForecastFetchedAt != nil && *w.ForecastFetchedAt != "" {
			t, err := time.Parse(time.RFC3339, *w.ForecastFetchedAt)
			if err != nil {
				return nil, fmt.Errorf("walks: walk %s forecast_fetched_at: %w", w.ID, err)
			}
			out.ForecastFetchedAt = t.UTC()
		}
		if w.DurationH > longWalkHours {
			out.shoulderHr = darkShoulderHours
		}
		out.Hours = out.Hours[:0]
		for _, h := range w.Hours {
			// A row of the wrong width is the wrong schema, which would
			// misread every walk, so it rejects the snapshot. An hour without
			// a time is one bad row: it is skipped and counted.
			if len(h) != width {
				return nil, fmt.Errorf("walks: walk %s: an hour has %d fields, want %d", w.ID, len(h), width)
			}
			if h[0] == nil {
				ds.SkippedHours++
				continue
			}
			at := time.Unix(int64(*h[0]), 0)
			// Older snapshots include the partial sunset hour. Keep only
			// complete daylight hours, matching hikes.forecast.is_daylight.
			// Preserve the observed forecast horizon before filtering.
			latest = max(latest, at.Unix())
			rise, set, ok := astro.SunTimes(at, w.Lat, w.Lon)
			if !ok || at.Before(rise) || at.Add(time.Hour).After(set) {
				continue
			}
			v := func(i int, missing float32) float32 {
				if i >= len(h) || h[i] == nil {
					return missing
				}
				return float32(*h[i])
			}
			// The hikes job never stores a null core value (it reads a missing
			// temperature, rain or wind as 0 and cloud as 50); a null here
			// gets the same reading.
			out.Hours = append(out.Hours, Hour{
				Unix: at.Unix(), Day: LocalDay(at),
				TempC: v(1, 0), PrecipMM: v(2, 0), WindKmh: v(3, 0), CloudPct: v(4, 50),
				FogPct: v(5, nan), LowCloudPct: v(6, nan),
			})
		}
		sort.Slice(out.Hours, func(i, j int) bool { return out.Hours[i].Unix < out.Hours[j].Unix })
		out.good = DefaultTest.filter(out.Hours)
		ds.Walks = append(ds.Walks, out)
	}
	if len(ds.Walks) == 0 {
		return nil, fmt.Errorf("walks: no usable walks in %d rows", len(d.Walks))
	}
	if !ds.FetchedAt.IsZero() {
		ds.HourlyUntil = ds.FetchedAt.Add(minHourlyHorizon)
	}
	if latest > 0 {
		if end := time.Unix(latest, 0).UTC().Add(time.Hour); end.After(ds.HourlyUntil) {
			ds.HourlyUntil = end
		}
	}
	ds.indexCoverage()
	return ds, nil
}

// Coverage says how much of a day's daylight the hourly forecast reaches.
type Coverage string

const (
	CoverageFull    Coverage = "full"
	CoveragePartial Coverage = "partial"
	CoverageNone    Coverage = "none"
)

func localHour(day int32, hour int) time.Time {
	y, m, d := time.Unix(int64(day)*86400, 0).UTC().Date()
	return time.Date(y, m, d, hour, 0, 0, 0, london)
}

// CoverageOf is how much of day's daylight lies before the hourly horizon.
func (d *Dataset) CoverageOf(day int32) Coverage {
	if !d.indexed {
		return d.coverage(day)
	}
	switch {
	case day <= d.fullThrough:
		return CoverageFull
	case day >= d.noneFrom:
		return CoverageNone
	}
	return CoveragePartial
}

// indexCoverage fills the CoverageOf index. Coverage only falls as days get
// later, so the two boundaries sit either side of the horizon's own day.
func (d *Dataset) indexCoverage() {
	h := LocalDay(d.HourlyUntil)
	d.fullThrough, d.noneFrom = h-2, h+2
	for day := h - 1; day <= h+1; day++ {
		switch d.coverage(day) {
		case CoverageFull:
			d.fullThrough = day
		case CoverageNone:
			d.noneFrom = min(d.noneFrom, day)
		}
	}
	d.indexed = true
}

func (d *Dataset) coverage(day int32) Coverage {
	return coverageUntil(day, d.HourlyUntil)
}

// Horizon is the earlier dataset/entity horizon. Missing metadata is unknown.
func (w *Walk) Horizon(datasetUntil time.Time) time.Time {
	if w.ForecastFetchedAt.IsZero() || datasetUntil.IsZero() {
		return time.Time{}
	}
	until := w.ForecastFetchedAt.Add(minHourlyHorizon)
	if datasetUntil.Before(until) {
		return datasetUntil
	}
	return until
}

func coverageUntil(day int32, until time.Time) Coverage {
	switch {
	case !until.Before(localHour(day, daylightEndHour)):
		return CoverageFull
	case !until.After(localHour(day, daylightStartHour)):
		return CoverageNone
	}
	return CoveragePartial
}

// folds maps the accented letters used in Scottish and Gaelic walk names to
// their plain forms, so "Dun Deardail" finds "Dùn Deardail".
var folds = strings.NewReplacer(
	"à", "a", "á", "a", "â", "a", "ä", "a", "è", "e", "é", "e", "ê", "e", "ë", "e",
	"ì", "i", "í", "i", "î", "i", "ï", "i", "ò", "o", "ó", "o", "ô", "o", "ö", "o",
	"ù", "u", "ú", "u", "û", "u", "ü", "u", "ç", "c", "ß", "ss", "ø", "o", "æ", "ae",
)

// Normalize lowercases, folds accents and turns everything but letters and
// digits into single spaces. Apostrophes separate words, so "a'Chearcaill"
// and "a' Chearcaill" both read "a chearcaill"; combining accents (a name
// stored decomposed) are dropped.
func Normalize(s string) string {
	s = folds.Replace(strings.ToLower(s))
	var b strings.Builder
	space := true
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			space = false
		case r >= 0x0300 && r <= 0x036f:
			// A combining accent: keep the letter it marks, drop the mark.
		default:
			if !space {
				b.WriteByte(' ')
				space = true
			}
		}
	}
	return strings.TrimSpace(b.String())
}

// Match scores a walk name against a normalized query: 0 is no match, 1 is
// every query word starting a word of the name, 2 is the name starting with
// the whole query ("ben nevis" against "Ben Nevis by the Mountain Path").
func (w *Walk) Match(query string) int { return w.match(query, strings.Fields(query)) }

// match is Match with the query already split into words, so a search over
// every walk splits it once.
func (w *Walk) match(query string, q []string) int {
	if len(q) == 0 {
		return 0
	}
	for _, t := range q {
		found := false
		for _, n := range w.nameTokens {
			if strings.HasPrefix(n, t) {
				found = true
				break
			}
		}
		if !found {
			return 0
		}
	}
	if strings.HasPrefix(w.nameKey, query) {
		return 2
	}
	return 1
}

// Run is the best slot on one day: its first and last good hour, how many
// good hours it holds (a tolerated bad hour inside it is not counted), and
// the weather across those good hours.
type Run struct {
	Start, End    time.Time
	GoodHours     int
	MaxWindKmh    float32
	TotalPrecipMM float32
	MaxPrecipMM   float32
	// WetHours counts good hours with at least WetMM of rain.
	WetHours int
	MinTempC float32
	MaxTempC float32
}

// WetMM is the rain in an hour that counts as wet rather than dry.
const WetMM = 0.1

// Day is one day at one walk. Decided is false when the forecast does not
// reach far enough to say: no coverage, or partial coverage without a slot
// long enough yet. Doable is only meaningful when Decided.
type Day struct {
	Day       int32
	Coverage  Coverage
	Decided   bool
	Doable    bool
	GoodHours int
	Best      *Run
	Hours     []Hour
	// From is the top of the hour hours count from (the next one, when the
	// day is under way), and Started reports that some of its good hours
	// had begun by then and were dropped. From, not the instant asked, so
	// that every answer within the hour is the same.
	From    time.Time
	Started bool
	// Limited reports that the caller's Limits turned at least one of the
	// day's hours away, and
	// All is the day's hours before they did: hours the limits turned away
	// are daylight with known weather, not darkness or bad weather.
	Limited bool
	All     []Hour
	// DarkLateOnly reports that, under limits, a long walk's dark credit can
	// only be spent after the slot, so it starts with the slot. GapFails is
	// what made the slot's one bad hour bad (see Test.Fails), empty when the
	// hour is not in the data.
	DarkLateOnly bool
	GapFails     []string
	// GapRainMM is the bad hour's rain when the data has the hour, else NaN.
	GapRainMM float32
	// Test is the good-hour test the day was assessed with.
	Test Test
}

// gapFails is what made the slot's one bad hour bad, and its rain, when the
// data has the hour.
func (w *Walk) gapFails(d *Day) ([]string, float32) {
	r := d.Best
	if int(r.End.Sub(r.Start)/time.Hour) == r.GoodHours {
		return nil, nan // no bad hour inside
	}
	// Hours are time-ordered: the gap is the first hour of the run that the
	// good hours skip.
	t := r.Start.Unix()
	for _, h := range d.Hours {
		if h.Unix < t {
			continue
		}
		if h.Unix != t {
			break
		}
		t += 3600
	}
	for _, h := range d.All {
		if h.Unix == t {
			return d.Test.Fails(h), h.PrecipMM
		}
	}
	return nil, nan
}

// limitedEdges keeps the dark-shoulder credit honest under limits. Without
// limits the hikes rule stands: a long walk may borrow two hours at either
// edge of its slot. Under limits the hours beside the slot are often daylight
// the person ruled out, so the credit counts only on a side that meets real
// darkness (the hour beyond the edge overlaps sunrise or sunset):
// with neither, the walk does not fit; with only the far side, it starts
// with the slot and finishes in the dark.
func (w *Walk) limitedEdges(d *Day, from time.Time) {
	if w.shoulderHr == 0 || !d.Doable || float64(d.Best.GoodHours) >= w.required-requirementEpsilon {
		return // the credit is not in play
	}
	rise, set, ok := astro.SunTimes(time.Unix(int64(d.Day)*86400, 0), w.Lat, w.Lon)
	if !ok {
		return
	}
	// Early darkness counts only while some of it is still to come: asked
	// about today after dawn, the dark before the slot is past.
	early := d.Best.Start.Add(-time.Hour).Before(rise) && rise.After(from)
	late := d.Best.End.Add(time.Hour).After(set)
	switch {
	case !early && !late:
		d.Doable = false
	case !early:
		d.DarkLateOnly = true
	}
}

// Limits change what counts as a good hour from the default test (at most
// DefaultMaxPrecipMM of rain and DefaultMaxWindKmh of wind, the hikes app's):
// for a caller who wants it drier, calmer or milder, or will put up with
// more. A nil field keeps the default. On a version 1 snapshot, which holds
// good hours only, a limit looser than the default would find no extra hours,
// so callers refuse it there (NeedsComplete).
type Limits struct {
	MaxPrecipMM, MaxWindKmh, MinTempC *float64
}

// The default good-hour test, the hikes job's viability rule
// (projects/monolith/hikes/forecast.py).
const (
	DefaultMaxPrecipMM = 2.0
	DefaultMaxWindKmh  = 80.0
)

// Active reports whether any limit is set.
func (l Limits) Active() bool {
	return l.MaxPrecipMM != nil || l.MaxWindKmh != nil || l.MinTempC != nil
}

// NeedsComplete reports limits only a version 2 snapshot can apply: looser
// than the default, since version 1 holds no hour that fails the default.
func (l Limits) NeedsComplete() bool {
	return l.MaxPrecipMM != nil && *l.MaxPrecipMM > DefaultMaxPrecipMM ||
		l.MaxWindKmh != nil && *l.MaxWindKmh > DefaultMaxWindKmh
}

// Test is a good-hour test: the defaults with the caller's limits applied.
type Test struct {
	MaxPrecipMM, MaxWindKmh, MinTempC float32
}

var inf = float32(math.Inf(1))

// DefaultTest is the hikes app's good-hour test.
var DefaultTest = Test{MaxPrecipMM: DefaultMaxPrecipMM, MaxWindKmh: DefaultMaxWindKmh, MinTempC: -inf}

// Test is the good-hour test these limits make. Values are compared at the
// hours' own float32 precision, so a limit of 0.2 mm admits an hour stored as
// 0.2 mm.
func (l Limits) Test() Test {
	t := DefaultTest
	set := func(dst *float32, v *float64) {
		if v != nil {
			*dst = float32(*v)
		}
	}
	set(&t.MaxPrecipMM, l.MaxPrecipMM)
	set(&t.MaxWindKmh, l.MaxWindKmh)
	set(&t.MinTempC, l.MinTempC)
	return t
}

// Fails lists what an hour fails: "rain", "wind", "cold".
func (t Test) Fails(h Hour) []string {
	var out []string
	if h.PrecipMM > t.MaxPrecipMM {
		out = append(out, "rain")
	}
	if h.WindKmh > t.MaxWindKmh {
		out = append(out, "wind")
	}
	if h.TempC < t.MinTempC {
		out = append(out, "cold")
	}
	return out
}

// Good reports whether an hour passes the test.
func (t Test) Good(h Hour) bool {
	return h.PrecipMM <= t.MaxPrecipMM && h.WindKmh <= t.MaxWindKmh && h.TempC >= t.MinTempC
}

func (t Test) filter(hours []Hour) []Hour {
	out := make([]Hour, 0, len(hours))
	for _, h := range hours {
		if t.Good(h) {
			out = append(out, h)
		}
	}
	return out
}

// longestRun is _longest_good_run: the longest run of good hours allowing
// one single-hour gap, as indices into hours (sorted, one day, unique hours).
// Bounds are inclusive; tied runs keep the earliest slot. A second gap slides
// the start past the previous gap, while two or more missing hours break it.
func longestRun(hours []Hour) (count, start, end int) {
	if len(hours) == 0 {
		return 0, 0, 0
	}
	best, bestStart, bestEnd := 1, 0, 0
	runStart, gapAfter := 0, -1
	for i := 1; i < len(hours); i++ {
		switch delta := (hours[i].Unix - hours[i-1].Unix) / 3600; {
		case delta == 1:
		case delta == 2:
			if gapAfter >= 0 {
				runStart = gapAfter
			}
			gapAfter = i
		default:
			runStart, gapAfter = i, -1
		}
		if run := i - runStart + 1; run > best {
			best, bestStart, bestEnd = run, runStart, i
		}
	}
	return best, bestStart, bestEnd
}

// dayHours is the part of hours (time-ordered) on day from cutoff on, and
// whether any of the day's hours were before cutoff.
func dayHours(hours []Hour, day int32, cutoff int64) ([]Hour, bool) {
	lo := sort.Search(len(hours), func(j int) bool { return hours[j].Day >= day })
	hi := sort.Search(len(hours), func(j int) bool { return hours[j].Day > day })
	from := sort.Search(hi-lo, func(j int) bool { return hours[lo+j].Unix >= cutoff })
	return hours[lo+from : hi], from > 0
}

// dayAt is the walk's assessed day: its good hours from cutoff (a Unix
// time, the next top of the hour) on, and whether it can be decided.
func (w *Walk) dayAt(day int32, cutoff int64, until time.Time, coverage Coverage, lim Limits) Day {
	all, _ := dayHours(w.Hours, day, cutoff)
	all = beforeHorizon(all, until)
	// The default test leaves a version 1 day as it is; a version 2 day
	// loses the hours that fail it.
	def, started := dayHours(w.good, day, cutoff)
	def = beforeHorizon(def, until)
	good, test := def, DefaultTest
	limited := false
	if lim.Active() {
		test = lim.Test()
		good = test.filter(all)
		// Started asks about the caller's good hours, not the default's.
		dayAll, _ := dayHours(w.Hours, day, 0)
		started = false
		for _, h := range dayAll {
			if h.Unix < cutoff && test.Good(h) {
				started = true
				break
			}
		}
		// Limited when the limits rule out an hour the default allows:
		// only then is there daylight the person turned away. Loosening
		// adds hours and rules none out, and a limit at the default changes
		// nothing, so neither changes the answer's rules.
		for _, h := range def {
			if !test.Good(h) {
				limited = true
				break
			}
		}
	}
	d := w.day(day, good)
	// The 80% rule can place the start before the good slot, but cannot
	// borrow elapsed daylight. Keep the weather tolerance, but a walk not
	// using dark credit must finish by sunset after the cutoff moves it.
	if d.Doable && float64(d.Best.GoodHours) >= w.required-requirementEpsilon {
		_, set, ok := astro.SunTimes(time.Unix(int64(day)*86400, 0), w.Lat, w.Lon)
		if ok && time.Unix(cutoff, 0).Add(time.Duration(w.DurationH*float64(time.Hour))).After(set) {
			d.Doable = false
		}
	}
	d.All = all
	d.Test = test
	d.Started = started
	d.Limited = limited
	if d.Limited && d.Best != nil {
		w.limitedEdges(&d, time.Unix(cutoff, 0))
	}
	d.GapRainMM = nan
	if d.Best != nil {
		d.GapFails, d.GapRainMM = w.gapFails(&d)
	}
	d.Coverage = coverage
	d.Decided = coverage == CoverageFull || d.Doable
	return d
}

// DayFor is one walk's assessed day as of now. Only hours that have not
// begun count: an hour already under way is not a full good hour left, and
// counting it would offer a start window in the past.
func (d *Dataset) DayFor(w *Walk, day int32, now time.Time) Day {
	return d.DayWithin(w, day, now, Limits{})
}

// DayWithin is DayFor with the caller's limits on what counts as a good hour.
func (d *Dataset) DayWithin(w *Walk, day int32, now time.Time, lim Limits) Day {
	cutoff := now.Truncate(time.Hour)
	if cutoff.Before(now) {
		cutoff = cutoff.Add(time.Hour)
	}
	until := w.Horizon(d.HourlyUntil)
	dw := w.dayAt(day, cutoff.Unix(), until, coverageUntil(day, until), lim)
	dw.From = cutoff
	return dw
}

func beforeHorizon(hours []Hour, until time.Time) []Hour {
	end := sort.Search(len(hours), func(i int) bool {
		return time.Unix(hours[i].Unix+3600, 0).After(until)
	})
	return hours[:end]
}

// ByID is the walk with id, or nil.
func (d *Dataset) ByID(id string) *Walk {
	for i := range d.Walks {
		if d.Walks[i].ID == id {
			return &d.Walks[i]
		}
	}
	return nil
}

// Named lists the walks matching a normalized name query, best match first
// (Match's score, then shorter names, then id).
func (d *Dataset) Named(query string) []*Walk {
	type hit struct {
		w     *Walk
		score int
	}
	var hits []hit
	words := strings.Fields(query)
	for i := range d.Walks {
		if s := d.Walks[i].match(query, words); s > 0 {
			hits = append(hits, hit{&d.Walks[i], s})
		}
	}
	sort.Slice(hits, func(i, j int) bool {
		a, b := hits[i], hits[j]
		if a.score != b.score {
			return a.score > b.score
		}
		if len(a.w.nameKey) != len(b.w.nameKey) {
			return len(a.w.nameKey) < len(b.w.nameKey)
		}
		return a.w.ID < b.w.ID
	})
	out := make([]*Walk, len(hits))
	for i, h := range hits {
		out[i] = h.w
	}
	return out
}

// DistanceKm is the distance from a point to the walk's start.
func (w *Walk) DistanceKm(lat, lon float64) float64 {
	dy := (w.Lat - lat) * kmPerDegLat
	dx := (w.Lon - lon) * kmPerDegLat * (math.Cos(lat*math.Pi/180) + w.cosLat) / 2
	return math.Sqrt(dx*dx + dy*dy)
}

func (w *Walk) day(day int32, hours []Hour) Day {
	d := Day{Day: day, GoodHours: len(hours), Hours: hours}
	n, s, e := longestRun(hours)
	if n == 0 {
		return d
	}
	r := &Run{
		Start: time.Unix(hours[s].Unix, 0).UTC(), End: time.Unix(hours[e].Unix, 0).UTC().Add(time.Hour), GoodHours: n,
		MinTempC: hours[s].TempC, MaxTempC: hours[s].TempC,
	}
	for _, h := range hours[s : e+1] {
		r.MaxWindKmh = max(r.MaxWindKmh, h.WindKmh)
		r.TotalPrecipMM += h.PrecipMM
		r.MaxPrecipMM = max(r.MaxPrecipMM, h.PrecipMM)
		if h.PrecipMM >= WetMM {
			r.WetHours++
		}
		r.MinTempC = min(r.MinTempC, h.TempC)
		r.MaxTempC = max(r.MaxTempC, h.TempC)
	}
	d.Best = r
	d.Doable = float64(n+w.shoulderHr) >= w.required-requirementEpsilon
	return d
}
