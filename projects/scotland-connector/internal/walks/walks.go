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
)

// SchemaVersion is the walks.json shape this package reads.
const SchemaVersion = 1

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

// DayName formats a LocalDay as YYYY-MM-DD.
func DayName(day int32) string {
	return time.Unix(int64(day)*86400, 0).UTC().Format("2006-01-02")
}

// Hour is one viable daylight hour at the walk's start point: present means
// it passed the daylight, precipitation and wind limits.
type Hour struct {
	Unix     int64
	Day      int32
	TempC    float32
	PrecipMM float32
	WindKmh  float32
	CloudPct float32
}

// Walk is one WalkHighlands walk with its viable hours in time order.
type Walk struct {
	ID         string
	Name       string
	URL        string
	Lat, Lon   float64
	LengthKm   float64
	AscentM    int
	DurationH  float64
	Hours      []Hour
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
	// snapshot. Both are lower bounds, so hours before it that are missing
	// failed the weather limits; hours after it were never forecast.
	HourlyUntil time.Time
	// Skipped counts snapshot rows left out for missing fields.
	Skipped int
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
		ID         string       `json:"id"`
		Name       string       `json:"name"`
		URL        string       `json:"url"`
		Lat        float64      `json:"lat"`
		Lon        float64      `json:"lon"`
		DistanceKm float64      `json:"distance_km"`
		AscentM    int          `json:"ascent_m"`
		DurationH  float64      `json:"duration_h"`
		Hours      [][5]float64 `json:"hours"`
	} `json:"walks"`
}

// Parse reads walks.json. version identifies the snapshot (its ETag).
func Parse(body []byte, version string) (*Dataset, error) {
	var d doc
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("walks: parse: %w", err)
	}
	if d.SchemaVersion != SchemaVersion {
		return nil, fmt.Errorf("walks: schema_version %d, want %d", d.SchemaVersion, SchemaVersion)
	}
	if len(d.Walks) == 0 {
		return nil, fmt.Errorf("walks: snapshot has no walks")
	}
	ds := &Dataset{Version: version, Walks: make([]Walk, 0, len(d.Walks))}
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
		if w.DurationH > longWalkHours {
			out.shoulderHr = darkShoulderHours
		}
		for i, h := range w.Hours {
			at := time.Unix(int64(h[0]), 0)
			out.Hours[i] = Hour{
				Unix: at.Unix(), Day: LocalDay(at),
				TempC: float32(h[1]), PrecipMM: float32(h[2]), WindKmh: float32(h[3]), CloudPct: float32(h[4]),
			}
			latest = max(latest, at.Unix())
		}
		sort.Slice(out.Hours, func(i, j int) bool { return out.Hours[i].Unix < out.Hours[j].Unix })
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
	switch {
	case !d.HourlyUntil.Before(localHour(day, daylightEndHour)):
		return CoverageFull
	case !d.HourlyUntil.After(localHour(day, daylightStartHour)):
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
	// Limited reports that the caller's Limits narrowed the good hours, and
	// All is the day's hours before they did: hours the limits turned away
	// are daylight with known weather, not darkness or bad weather.
	Limited bool
	All     []Hour
	// NoEarlyStart reports that the hour before the slot is daylight the
	// limits turned away, so a walk that needs more than the slot cannot
	// start early in the dark; GapOutsideLimits that the slot's one bad hour
	// is such an hour.
	NoEarlyStart     bool
	GapOutsideLimits bool
}

// limitedEdges applies the dark-shoulder credit only where the slot can
// meet darkness: an hour the limits turned away is daylight the person ruled
// out. With both edges turned away the credit is void.
func (w *Walk) limitedEdges(d *Day) {
	good := make(map[int64]bool, len(d.Hours))
	for _, h := range d.Hours {
		good[h.Unix] = true
	}
	turnedAway := func(t int64) bool {
		for _, h := range d.All {
			if h.Unix == t {
				return !good[t]
			}
		}
		return false
	}
	start, end := d.Best.Start.Unix(), d.Best.End.Unix()
	d.NoEarlyStart = turnedAway(start - 3600)
	if d.NoEarlyStart && turnedAway(end) {
		d.Doable = float64(d.Best.GoodHours) >= w.required-requirementEpsilon
	}
	for t := start; t < end; t += 3600 {
		if !good[t] && turnedAway(t) {
			d.GapOutsideLimits = true
		}
	}
}

// Limits tighten what counts as a good hour beyond the snapshot's own test
// (at most ViableMaxPrecipMM of rain and ViableMaxWindKmh of wind): for a
// caller who wants it dry, calm or mild. A nil field leaves that test alone.
// The snapshot holds good hours only, so limits can tighten the test but
// never loosen it.
type Limits struct {
	MaxPrecipMM, MaxWindKmh, MinTempC *float64
}

// The snapshot's own good-hour test, applied by the hikes job before an hour
// is stored (projects/monolith/hikes/forecast.py).
const (
	ViableMaxPrecipMM = 2.0
	ViableMaxWindKmh  = 80.0
)

// Active reports whether any limit is set.
func (l Limits) Active() bool {
	return l.MaxPrecipMM != nil || l.MaxWindKmh != nil || l.MinTempC != nil
}

// allows compares at the hours' own float32 precision, so a limit of 0.2 mm
// admits an hour stored as 0.2 mm.
func (l Limits) allows(h Hour) bool {
	return (l.MaxPrecipMM == nil || h.PrecipMM <= float32(*l.MaxPrecipMM)) &&
		(l.MaxWindKmh == nil || h.WindKmh <= float32(*l.MaxWindKmh)) &&
		(l.MinTempC == nil || h.TempC >= float32(*l.MinTempC))
}

// longestRun is _longest_good_run: the longest run of good hours allowing
// one single-hour gap, as indices into hours (sorted, one day, unique hours).
func longestRun(hours []Hour) (count, start, end int) {
	if len(hours) == 0 {
		return 0, 0, 0
	}
	best, bestStart, bestEnd := 0, 0, 0
	run, runStart, gapUsed := 1, 0, false
	for i := 1; i < len(hours); i++ {
		switch delta := (hours[i].Unix - hours[i-1].Unix) / 3600; {
		case delta == 1:
			run++
		case delta == 2 && !gapUsed:
			run++
			gapUsed = true
		default:
			if run > best {
				best, bestStart, bestEnd = run, runStart, i-1
			}
			run, runStart, gapUsed = 1, i, false
		}
	}
	if run > best {
		best, bestStart, bestEnd = run, runStart, len(hours)-1
	}
	return best, bestStart, bestEnd
}

// dayAt is the walk's assessed day: its good hours from cutoff (a Unix
// time, the next top of the hour) on, and whether it can be decided.
func (w *Walk) dayAt(day int32, cutoff int64, coverage Coverage, lim Limits) Day {
	hours := w.Hours
	lo := sort.Search(len(hours), func(j int) bool { return hours[j].Day >= day })
	hi := sort.Search(len(hours), func(j int) bool { return hours[j].Day > day })
	dayHours := hours[lo:hi]
	started := len(dayHours) > 0 && dayHours[0].Unix < cutoff
	for len(dayHours) > 0 && dayHours[0].Unix < cutoff {
		dayHours = dayHours[1:]
	}
	all := dayHours
	if lim.Active() {
		kept := make([]Hour, 0, len(dayHours))
		for _, h := range dayHours {
			if lim.allows(h) {
				kept = append(kept, h)
			}
		}
		dayHours = kept
	}
	d := w.day(day, dayHours)
	d.All = all
	d.Started = started
	d.Limited = lim.Active()
	if d.Limited && d.Best != nil {
		w.limitedEdges(&d)
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
	dw := w.dayAt(day, cutoff.Unix(), d.CoverageOf(day), lim)
	dw.From = cutoff
	return dw
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
