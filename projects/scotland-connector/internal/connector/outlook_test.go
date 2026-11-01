package connector_test

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

func outlook(t *testing.T, svc *connector.Service, from, to string) connector.OutlookResult {
	t.Helper()
	lat, lon := 57.3, -3.7
	env, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, From: &from, To: &to})
	if err != nil {
		t.Fatal(err)
	}
	if env.Meta.ForecastFetchedAt != nil || len(env.Meta.Sources) != 2 || !strings.Contains(env.Meta.Sources[0].Name, "CERRA") || !strings.Contains(env.Meta.Sources[1].Name, "Local astronomical") {
		t.Fatal(env.Meta)
	}
	return env.Data.(connector.OutlookResult)
}

func climateService() *connector.Service {
	ds := fixture.Dataset()
	for i := range ds.Sites {
		ds.Sites[i].HasClimate = false
		ds.Sites[i].Hours = nil
		ds.Sites[i].ForecastFetchedAt = time.Time{}
	}
	a, b := &ds.Sites[0], &ds.Sites[1]
	a.HasClimate = true
	b.HasClimate = true
	for m := range 12 {
		a.ClimateClear[m], a.ClimateDark[m] = 60, 300
		b.ClimateClear[m], b.ClimateDark[m] = 90, 600
	}
	// June's unsupported 100% must not beat winter's supported 20%.
	a.ClimateClear[5], a.ClimateDark[5] = 299, 299
	b.ClimateClear[5], b.ClimateDark[5] = 298, 299
	// April wins by rate, January by absolute historical hours.
	a.ClimateClear[3] = 120
	b.ClimateClear[0] = 180
	svc := fixture.Service(false)
	svc.Data = fixture.Source{DS: ds}
	return svc
}

func TestOutlookHistoricalWinnersAndFloor(t *testing.T) {
	r := outlook(t, climateService(), "2027-01-01", "2027-12-31")
	if len(r.Months) != 12 || r.RateRanking[0] != "2027-04" || r.HoursRanking[0] != "2027-06" {
		t.Fatal(r.RateRanking, r.HoursRanking)
	}
	jan, jun := r.Months[0], r.Months[5]
	if jan.RateWinner.Site.ID != "scotland-0002" || jan.HoursWinner.Site.ID != "scotland-0002" {
		t.Fatal(jan)
	}
	// Most months deliberately disagree about which site is best.
	feb := r.Months[1]
	if feb.RateWinner.Site.ID != "scotland-0001" || feb.HoursWinner.Site.ID != "scotland-0002" || feb.RateWinner.DarkHours != 300 {
		t.Fatal(feb)
	}
	if jun.RateWinner != nil || jun.RateRank != nil || jun.HoursWinner.Share != nil || jun.HoursWinner.ClearDarkHours != 299 {
		t.Fatal(jun)
	}
	for _, m := range r.Months {
		if m.Verdict != "unknown" || !strings.Contains(m.Why, "climatology") || !strings.Contains(m.Why, m.Month) {
			t.Fatal(m)
		}
	}
	if r.Verdict != "unknown" || jan.RateRank == nil {
		t.Fatal(r)
	}
	// Change only January's absolute winner so January wins hours overall.
	svc := climateService()
	ds := svc.Data.Current()
	ds.Sites[1].ClimateClear[0] = 400
	r = outlook(t, svc, "2027-01-01", "2027-12-31")
	if r.HoursRanking[0] != "2027-01" || r.RateRanking[0] != "2027-01" {
		t.Fatal(r.HoursRanking, r.RateRanking)
	}
	// At 300 support rate exists; at 299 it is unknown, with no invented 0.
	ds.Sites[0].ClimateDark[1] = 299
	ds.Sites[1].HasClimate = false
	r = outlook(t, svc, "2027-02-01", "2027-02-01")
	if r.Days[0].RateRank != nil || r.Months[0].RateWinner != nil {
		t.Fatal(r)
	}
	ds.Sites[0].ClimateDark[1] = 300
	r = outlook(t, svc, "2027-02-01", "2027-02-01")
	if r.Days[0].RateRank == nil || *r.Days[0].RateRank != 1 {
		t.Fatal(r)
	}
}

func TestOutlookSeparateRankings(t *testing.T) {
	svc := climateService()
	ds := svc.Data.Current()
	ds.Sites[1].ClimateClear[0] = 150
	r := outlook(t, svc, "2027-01-01", "2027-04-30")
	if r.RateRanking[0] != "2027-04" || r.HoursRanking[0] != "2027-01" {
		t.Fatal(r.RateRanking, r.HoursRanking)
	}
}

func TestOutlookCandidateRadiusAndTies(t *testing.T) {
	// Reverse ID order in the snapshot, with identical coordinates/scores.
	// The distant winner is outside the default radius but inside 100 km.
	ds, err := stars.Parse([]byte(`{"sites":[{"id":"b","lat":57.3,"lon":-3.7},{"id":"a","lat":57.3,"lon":-3.7},{"id":"far","lat":57.9,"lon":-3.7}]}`), []byte(`{"sites":[
	{"id":"a","clear":[60,60,60,60,60,60,60,60,60,60,60,60],"dark":[300,300,300,300,300,300,300,300,300,300,300,300]},
	{"id":"b","clear":[60,60,60,60,60,60,60,60,60,60,60,60],"dark":[300,300,300,300,300,300,300,300,300,300,300,300]},
	{"id":"far","clear":[270,270,270,270,270,270,270,270,270,270,270,270],"dark":[300,300,300,300,300,300,300,300,300,300,300,300]}]}`), "ties")
	if err != nil {
		t.Fatal(err)
	}
	svc := fixture.Service(false)
	svc.Data = fixture.Source{DS: ds}
	r := outlook(t, svc, "2027-01-31", "2027-02-01")
	if r.Months[0].RateWinner.Site.ID != "a" || r.Months[0].HoursWinner.Site.ID != "a" || *r.Months[0].RateRank != 1 || *r.Months[1].RateRank != 1 {
		t.Fatal(r)
	}
	if strings.Join(r.RateRanking, ",") != "2027-01,2027-02" {
		t.Fatal(r.RateRanking)
	}
	lat, lon, radius := 57.3, -3.7, 100.0
	date := "2027-01-01"
	env, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, RadiusKm: &radius, From: &date, To: &date})
	if err != nil {
		t.Fatal(err)
	}
	m := env.Data.(connector.OutlookResult).Months[0]
	if m.RateWinner.Site.ID != "far" || m.HoursWinner.Site.ID != "far" {
		t.Fatal(m)
	}
	// Corrupt historical counts never fabricate a rank or a percentage.
	for i := range ds.Sites {
		ds.Sites[i].ClimateClear[0] = 301
	}
	r = outlook(t, svc, date, date)
	if r.Months[0].RateWinner != nil || r.Months[0].HoursWinner != nil {
		t.Fatal(r)
	}
}

func TestOutlookMissingClimateAndSites(t *testing.T) {
	for _, svc := range []*connector.Service{fixture.Service(false), fixture.Service(true)} {
		if svc.Data.Current() != nil {
			for i := range svc.Data.Current().Sites {
				svc.Data.Current().Sites[i].HasClimate = false
			}
		}
		r := outlook(t, svc, "2027-01-01", "2027-01-02")
		m := r.Months[0]
		if m.RateWinner != nil || m.HoursWinner != nil || m.RateRank != nil || m.HoursRank != nil || len(r.RateRanking) != 0 || r.Days[0].Verdict != "unknown" || m.DarkHours <= 0 {
			t.Fatal(r)
		}
	}
	// A loaded dataset with climate elsewhere still has no nearby coverage.
	svc := climateService()
	lat, lon, radius := 55.0, -8.0, 1.0
	date := "2027-01-01"
	env, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, RadiusKm: &radius, From: &date, To: &date})
	if err != nil || env.Data.(connector.OutlookResult).Months[0].HoursWinner != nil {
		t.Fatal(env, err)
	}
}

func TestOutlookDateLimitsAndDetail(t *testing.T) {
	for _, tc := range []struct {
		from, to string
		valid    bool
		days     int
	}{
		{"2027-01-01", "2027-12-31", true, 365},
		{"2027-01-01", "2028-01-01", false, 0},
		{"2028-01-01", "2028-12-31", true, 366},
		{"2028-02-29", "2029-02-27", true, 365},
		{"2028-02-29", "2029-02-28", false, 0},
		{"2027-10-01", "2028-09-30", true, 366},
		{"2027-10-01", "2028-10-01", false, 0},
		{"2027-01-01", "2027-04-02", true, 92},
		{"2027-01-01", "2027-04-03", true, 93},
		{"2027-01-02", "2027-01-01", false, 0},
		{"2027-02-29", "2027-03-01", false, 0},
		{"1999-12-31", "2000-01-01", false, 0},
		{"2100-12-31", "2101-01-01", false, 0},
	} {
		t.Run(tc.from+"_"+tc.to, func(t *testing.T) {
			lat, lon := 57.3, -3.7
			env, err := fixture.Service(false).Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, From: &tc.from, To: &tc.to})
			if (err == nil) != tc.valid {
				t.Fatal(err)
			}
			if err != nil {
				return
			}
			r := env.Data.(connector.OutlookResult)
			if r.DailyDetail.RequestedDates != tc.days {
				t.Fatal(r.DailyDetail)
			}
			if tc.days > connector.OutlookDailyCap {
				if len(r.Days) != 0 || r.DailyDetail.Status != "omitted" || r.DailyDetail.ReturnedDates != 0 || r.Months[0].DarkHours <= 0 {
					t.Fatal(r)
				}
			} else if len(r.Days) != tc.days || r.DailyDetail.Status != "full" {
				t.Fatal(r.DailyDetail)
			}
		})
	}
}

func TestOutlookGeometryAndAggregation(t *testing.T) {
	for _, span := range [][2]string{{"2028-02-28", "2028-03-01"}, {"2027-03-27", "2027-03-29"}, {"2027-10-30", "2027-11-01"}, {"2027-12-31", "2028-01-02"}, {"2027-06-20", "2027-06-22"}, {"2027-07-06", "2027-07-08"}} {
		r := outlook(t, climateService(), span[0], span[1])
		dark, moonless := map[string]float64{}, map[string]float64{}
		for _, d := range r.Days {
			start, _ := time.ParseInLocation(time.DateOnly, d.Date, timeLondon(t))
			end := start.AddDate(0, 0, 1)
			ref, _ := time.Parse(time.RFC3339, d.ReferenceAt)
			if ref.In(timeLondon(t)).Hour() != 12 || math.Abs(d.Phase-astro.MoonAt(ref).Phase()) > 0.000051 || math.Abs(d.Illumination-astro.MoonIllumination(ref)) > 0.000051 {
				t.Fatal(d)
			}
			var seconds float64
			var prev time.Time
			for _, w := range d.Moonless {
				a, _ := time.Parse(time.RFC3339, w.From)
				b, _ := time.Parse(time.RFC3339, w.Until)
				if a.Before(start) || b.After(end) || !a.Before(b) || a.Before(prev) {
					t.Fatal(d, w)
				}
				prev = b
				mid := a.Add(b.Sub(a) / 2)
				if astro.SunElevation(mid, 57.3, -3.7) > -11.999 || astro.MoonAltitude(mid, 57.3, -3.7) > -0.832 {
					t.Fatal(d, w)
				}
				seconds += b.Sub(a).Seconds()
			}
			if math.Abs(seconds/3600-d.MoonlessDarkHours) > 0.000051 || d.MoonlessDarkHours > d.DarkHours || d.DarkHours > end.Sub(start).Hours() {
				t.Fatal(d)
			}
			dark[d.Date[:7]] += d.DarkHours
			moonless[d.Date[:7]] += d.MoonlessDarkHours
			// An independent one-minute midpoint integration bounds both
			// geometry totals, including absent crossings and DST days.
			var scanDark, scanMoon float64
			for at := start.Add(30 * time.Second); at.Before(end); at = at.Add(time.Minute) {
				if astro.SunElevation(at, 57.3, -3.7) < -12 {
					scanDark += 1.0 / 60
					if astro.MoonAltitude(at, 57.3, -3.7) < -0.833 {
						scanMoon += 1.0 / 60
					}
				}
			}
			if math.Abs(scanDark-d.DarkHours) > 0.04 || math.Abs(scanMoon-d.MoonlessDarkHours) > 0.04 {
				t.Fatal(d, scanDark, scanMoon)
			}
		}
		for _, m := range r.Months {
			if math.Abs(dark[m.Month]-m.DarkHours) > 0.001 || math.Abs(moonless[m.Month]-m.MoonlessDarkHours) > 0.001 {
				t.Fatal(m)
			}
		}
	}
	// Summer zero darkness is a known geometric zero, independent of history.
	lat, lon := 60.3, -1.3
	date := "2027-06-21"
	env, err := fixture.Service(false).Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, From: &date, To: &date})
	if err != nil {
		t.Fatal(err)
	}
	d := env.Data.(connector.OutlookResult).Days[0]
	if d.DarkHours != 0 || d.MoonlessDarkHours != 0 || len(d.Moonless) != 0 {
		t.Fatal(d)
	}
}

func TestOutlookOmittedDetailKeepsCompleteMonths(t *testing.T) {
	svc := climateService()
	full := outlook(t, svc, "2027-01-01", "2027-04-02")
	last := outlook(t, svc, "2027-04-03", "2027-04-03")
	omitted := outlook(t, svc, "2027-01-01", "2027-04-03")
	for i, m := range omitted.Months {
		dh, mh := full.Months[i].DarkHours, full.Months[i].MoonlessDarkHours
		if m.Month == "2027-04" {
			dh += last.Months[0].DarkHours
			mh += last.Months[0].MoonlessDarkHours
		}
		if math.Abs(dh-m.DarkHours) > 0.00011 || math.Abs(mh-m.MoonlessDarkHours) > 0.00011 {
			t.Fatal(m, dh, mh)
		}
		if m.HoursWinner.ClearDarkHours != full.Months[i].HoursWinner.ClearDarkHours {
			t.Fatal("partial months prorated archive totals")
		}
	}
}

func TestOutlookDefaultsAndBounds(t *testing.T) {
	svc := fixture.Service(false)
	lat, lon := 57.3, -3.7
	// UTC June 30 is already July 1 in London.
	svc.Now = func() time.Time { return time.Date(2027, 6, 30, 23, 30, 0, 0, time.UTC) }
	env, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon})
	if err != nil {
		t.Fatal(err)
	}
	q := env.Data.(connector.OutlookResult).Query
	if q.From != "2027-07-01" || q.To != "2028-06-30" {
		t.Fatal(q)
	}
	from := "2100-12-31"
	env, err = svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, From: &from})
	if err != nil || env.Data.(connector.OutlookResult).Query.To != from {
		t.Fatal(env, err)
	}
	for _, radius := range []float64{math.NaN(), math.Inf(1), 0, 100.1} {
		_, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, RadiusKm: &radius})
		if err == nil {
			t.Fatal(radius)
		}
	}
}

func BenchmarkOutlookYear(b *testing.B) {
	svc := climateService()
	lat, lon := 57.3, -3.7
	from, to := "2028-01-01", "2028-12-31"
	b.ResetTimer()
	for range b.N {
		if _, err := svc.Outlook(connector.OutlookParams{Lat: &lat, Lon: &lon, From: &from, To: &to}); err != nil {
			b.Fatal(err)
		}
	}
}

func timeLondon(t *testing.T) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation("Europe/London")
	if err != nil {
		t.Fatal(err)
	}
	return loc
}
