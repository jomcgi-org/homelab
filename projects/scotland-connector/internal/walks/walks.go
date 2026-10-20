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
	switch {
	case !d.HourlyUntil.Before(localHour(day, daylightEndHour)):
		return CoverageFull
	case !d.HourlyUntil.After(localHour(day, daylightStartHour)):
		return CoverageNone
	}
	return CoveragePartial
}

// Query is one walk_windows question; the connector validates it.
type Query struct {
	// HasPoint is false for a name-only search, which has no radius and no
	// distances.
	HasPoint     bool
	Lat, Lon     float64
	RadiusKm     float64
	Days         int
	Limit        int
	MaxDurationH float64 // 0 means no limit
	MaxAscentM   int     // 0 means no limit
	// Date, when set, is the one day asked about (a LocalDay); Days is then 1.
	Date int32
	// Name, when set, keeps only walks whose name matches (see Match).
	Name string
	Now  time.Time
}

// FirstDay is the query's first day: Date, or today in UK time.
func (q Query) FirstDay() int32 {
	if q.Date != 0 {
		return q.Date
	}
	return LocalDay(q.Now)
}

// folds maps the accented letters used in Scottish and Gaelic walk names to
// their plain forms, so "Dun Deardail" finds "Dùn Deardail".
var folds = strings.NewReplacer(
	"à", "a", "á", "a", "â", "a", "ä", "a", "è", "e", "é", "e", "ê", "e", "ë", "e",
	"ì", "i", "í", "i", "î", "i", "ï", "i", "ò", "o", "ó", "o", "ô", "o", "ö", "o",
	"ù", "u", "ú", "u", "û", "u", "ü", "u", "ç", "c", "’", "'",
)

// Normalize lowercases, folds accents and turns everything but letters and
// digits into single spaces.
func Normalize(s string) string {
	s = folds.Replace(strings.ToLower(s))
	var b strings.Builder
	space := true
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			space = false
		case r == '\'':
			// Apostrophes join: "a' Chearcaill" and "a Chearcaill" both match.
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
func (w *Walk) Match(query string) int {
	q := strings.Fields(query)
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

// DayNames lists the covered days.
func (q Query) DayNames() []string {
	first := q.FirstDay()
	out := make([]string, q.Days)
	for i := range out {
		out[i] = DayName(first + int32(i))
	}
	return out
}

// Run is the best slot on one day: its first and last good hour, how many
// good hours it holds (a tolerated bad hour inside it is not counted), and
// the weather across those good hours.
type Run struct {
	Start, End    time.Time
	GoodHours     int
	MaxWindKmh    float32
	TotalPrecipMM float32
	MinTempC      float32
	MaxTempC      float32
}

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
}

// Result is one ranked walk.
type Result struct {
	Walk *Walk
	// DistanceKm is from the query point; meaningless without HasPoint.
	DistanceKm float64
	// NameScore is Match's score for a name query, else 0.
	NameScore  int
	DoableDays int
	Days       []Day
	firstOK    int32
	goodHours  int
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
		r.MinTempC = min(r.MinTempC, h.TempC)
		r.MaxTempC = max(r.MaxTempC, h.TempC)
	}
	d.Best = r
	d.Doable = float64(n+w.shoulderHr) >= w.required-requirementEpsilon
	return d
}

// FirstDoable is the earliest doable day, if any.
func (r *Result) FirstDoable() (Day, bool) {
	for _, d := range r.Days {
		if d.Doable {
			return d, true
		}
	}
	return Day{}, false
}

func better(a, b *Result) bool {
	if a.NameScore != b.NameScore {
		return a.NameScore > b.NameScore
	}
	if a.DoableDays != b.DoableDays {
		return a.DoableDays > b.DoableDays
	}
	if a.firstOK != b.firstOK {
		return a.firstOK < b.firstOK
	}
	if a.goodHours != b.goodHours {
		return a.goodHours > b.goodHours
	}
	if a.DistanceKm != b.DistanceKm {
		return a.DistanceKm < b.DistanceKm
	}
	return a.Walk.ID < b.Walk.ID
}

// Windows ranks the walks within the radius: most doable days first, then
// the earliest doable day, then the most good hours, then the nearest.
func (d *Dataset) Windows(q Query) []Result {
	first := q.FirstDay()
	last := first + int32(q.Days) - 1
	cutoff := q.Now.Truncate(time.Hour).Unix()
	cosQ := math.Cos(q.Lat * math.Pi / 180)
	coverage := make([]Coverage, q.Days)
	for i := range coverage {
		coverage[i] = d.CoverageOf(first + int32(i))
	}
	var out []Result
	for i := range d.Walks {
		w := &d.Walks[i]
		if q.MaxDurationH > 0 && w.DurationH > q.MaxDurationH {
			continue
		}
		if q.MaxAscentM > 0 && w.AscentM > q.MaxAscentM {
			continue
		}
		score := 0
		if q.Name != "" {
			if score = w.Match(q.Name); score == 0 {
				continue
			}
		}
		var dist float64
		if q.HasPoint {
			dy := (w.Lat - q.Lat) * kmPerDegLat
			dx := (w.Lon - q.Lon) * kmPerDegLat * (cosQ + w.cosLat) / 2
			dist = math.Sqrt(dx*dx + dy*dy)
			if dist > q.RadiusKm {
				continue
			}
		}
		r := Result{Walk: w, DistanceKm: dist, NameScore: score, Days: make([]Day, 0, q.Days), firstOK: math.MaxInt32}
		hours := w.Hours
		for day := first; day <= last; day++ {
			lo := sort.Search(len(hours), func(j int) bool { return hours[j].Day >= day })
			hi := sort.Search(len(hours), func(j int) bool { return hours[j].Day > day })
			dayHours := hours[lo:hi]
			for len(dayHours) > 0 && dayHours[0].Unix < cutoff {
				dayHours = dayHours[1:]
			}
			dw := w.day(day, dayHours)
			dw.Coverage = coverage[day-first]
			dw.Decided = dw.Coverage == CoverageFull || dw.Doable
			if dw.Doable {
				r.DoableDays++
				if day < r.firstOK {
					r.firstOK = day
				}
			}
			r.goodHours += dw.GoodHours
			r.Days = append(r.Days, dw)
		}
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return better(&out[i], &out[j]) })
	if len(out) > q.Limit {
		out = out[:q.Limit]
	}
	return out
}
