package stars

import (
	"math"
	"sort"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	_ "time/tzdata" // the image ships no zoneinfo; nights are Scottish local dates
)

var london = mustLoad("Europe/London")

func mustLoad(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}

// NightDay numbers the night an instant belongs to by the local calendar date
// of its evening, as days since 1970-01-01: 01:00 on the 20th is part of the
// night of the 19th. Local noon is the boundary.
func NightDay(t time.Time) int32 {
	y, m, d := t.In(london).Add(-12 * time.Hour).Date()
	return int32(time.Date(y, m, d, 0, 0, 0, 0, time.UTC).Unix() / 86400)
}

// NightName formats a NightDay as its evening's date, YYYY-MM-DD.
func NightName(day int32) string {
	return time.Unix(int64(day)*86400, 0).UTC().Format(time.DateOnly)
}

// NightOf names the night an instant belongs to.
func NightOf(t time.Time) string { return NightName(NightDay(t)) }

// WindowsQuery asks for the best sites near a point over the next few nights.
type WindowsQuery struct {
	Lat, Lon float64
	RadiusKm float64
	Nights   int
	Limit    int
	// Night, when set, is the one night asked about (a NightDay); Nights is
	// then 1. Zero means the nights from tonight.
	Night int32
	// Now is the query clock. Hours before the top of its hour are dropped,
	// matching the monolith's read-time filter.
	Now time.Time
}

// MoonlessMaxLit is the lit fraction below which the moon does not count:
// a thin crescent barely brightens the sky.
const MoonlessMaxLit = 0.1

// NightWindow is one night's clear hours at a site.
type NightWindow struct {
	Night         string
	ClearDark     int
	ClearTwilight int
	// MoonlessDark counts clear-dark hours with the moon below the horizon
	// or less than MoonlessMaxLit lit.
	MoonlessDark int
	// MoonLit is the moon's lit fraction at local midnight of the night.
	MoonLit float64
	Hours   []Hour
	// MoonAlt is the moon's altitude in degrees at each of Hours.
	MoonAlt []float32
}

// Climate is a site's climatology for one calendar month.
type Climate struct {
	Month     int
	ClearDark int
	Dark      int
}

// Window is one ranked site.
type Window struct {
	Site          *Site
	DistanceKm    float64
	ClearDark     int
	ClearTwilight int
	Nights        []NightWindow
	MoonlessDark  int
	Climate       *Climate
}

// lpRank orders light-pollution zones darkest first.
func lpRank(zone string) int {
	switch zone {
	case "pristine":
		return 0
	case "excellent":
		return 1
	case "rural":
		return 2
	default:
		return 3
	}
}

// morningCutover is how long after local midnight the previous night stops
// being "tonight" for a query. No Scottish site has a twilight-dark hour (sun
// below -10 deg) after about 08:10 local, even at midwinter in Shetland, so
// from 09:00 the night still in progress has nothing left to offer and the
// coming evening is the first night asked about.
const morningCutover = 9 * time.Hour

// FirstNight is the first night a query starting at now covers: the night in
// progress until the morning cutover, then the coming evening's.
func FirstNight(now time.Time) int32 {
	return NightDay(now.Add(12*time.Hour - morningCutover))
}

// First is the query's first night.
func (q WindowsQuery) First() int32 {
	if q.Night != 0 {
		return q.Night
	}
	return FirstNight(q.Now)
}

// NightNames lists the nights a query covers, starting with tonight.
func (q WindowsQuery) NightNames() []string {
	first := q.First()
	names := make([]string, q.Nights)
	for i := range names {
		names[i] = NightName(first + int32(i))
	}
	return names
}

// better is the ranking order: more clear-dark hours, then more
// clear-twilight hours (the midsummer fallback), then a darker light-pollution
// zone, then nearer, then id so the order is total and stable.
func better(a, b *Window) bool {
	if a.ClearDark != b.ClearDark {
		return a.ClearDark > b.ClearDark
	}
	if a.ClearTwilight != b.ClearTwilight {
		return a.ClearTwilight > b.ClearTwilight
	}
	if ra, rb := lpRank(a.Site.LPZone), lpRank(b.Site.LPZone); ra != rb {
		return ra < rb
	}
	if math.Abs(a.DistanceKm-b.DistanceKm) > 1e-9 {
		return a.DistanceKm < b.DistanceKm
	}
	return a.Site.ID < b.Site.ID
}

// Windows returns the best q.Limit sites within the radius over the requested
// nights, in the order defined by better.
func (d *Dataset) Windows(q WindowsQuery) []Window {
	names := q.NightNames()
	first := q.First()
	last := first + int32(q.Nights)
	cutoff := q.Now.UTC().Truncate(time.Hour).Unix()
	month := int(time.Unix(int64(first)*86400, 0).UTC().Month())

	// Bounded top-k selection: the limit is at most a handful, so keeping a
	// sorted window of k beats sorting every candidate in a wide radius.
	top := make([]Window, 0, q.Limit+1)
	d.visit(q.Lat, q.Lon, q.RadiusKm, func(site *Site, dist float64) {
		w := Window{Site: site, DistanceKm: dist}
		hours := site.Hours
		for i := sort.Search(len(hours), func(i int) bool { return hours[i].Unix >= cutoff }); i < len(hours); i++ {
			h := hours[i]
			if h.Night >= last {
				break // hours are time-ordered, so every later hour is too
			}
			if h.Night < first {
				continue
			}
			w.ClearTwilight++
			if h.Dark {
				w.ClearDark++
			}
		}
		if len(top) == q.Limit && !better(&w, &top[len(top)-1]) {
			return
		}
		pos := sort.Search(len(top), func(i int) bool { return better(&w, &top[i]) })
		top = append(top, Window{})
		copy(top[pos+1:], top[pos:])
		top[pos] = w
		if len(top) > q.Limit {
			top = top[:q.Limit]
		}
	})
	windows := top

	// The moon's phase is the same for every site; its altitude is not.
	moonLit := make([]float64, len(names))
	for j := range moonLit {
		y, m, dd := time.Unix(int64(first+int32(j)+1)*86400, 0).UTC().Date()
		moonLit[j] = astro.MoonAt(time.Date(y, m, dd, 0, 0, 0, 0, london)).Lit
	}

	// Only the returned few pay for the per-night breakdown, and they share
	// one moon position per hour.
	moons := map[int64]astro.Moon{}
	for i := range windows {
		w := &windows[i]
		w.Nights = make([]NightWindow, len(names))
		for j, n := range names {
			w.Nights[j].Night = n
			w.Nights[j].MoonLit = moonLit[j]
		}
		for _, h := range w.Site.Hours {
			if h.Unix < cutoff || h.Night < first || h.Night >= last {
				continue
			}
			nw := &w.Nights[h.Night-first]
			nw.ClearTwilight++
			moon, ok := moons[h.Unix]
			if !ok {
				moon = astro.MoonAt(time.Unix(h.Unix, 0))
				moons[h.Unix] = moon
			}
			alt := moon.Altitude(w.Site.Lat, w.Site.Lon)
			if h.Dark {
				nw.ClearDark++
				if alt < 0 || moon.Lit < MoonlessMaxLit {
					nw.MoonlessDark++
					w.MoonlessDark++
				}
			}
			nw.Hours = append(nw.Hours, h)
			nw.MoonAlt = append(nw.MoonAlt, float32(alt))
		}
		if w.Site.HasClimate {
			w.Climate = &Climate{Month: month, ClearDark: w.Site.ClimateClear[month-1], Dark: w.Site.ClimateDark[month-1]}
		}
	}
	return windows
}
