package connector_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

func f(v float64) *float64 { return &v }
func i(v int) *int         { return &v }
func s(v string) *string   { return &v }

func plan(t *testing.T, p connector.PlanParams) (*connector.Envelope, connector.PlanResult) {
	t.Helper()
	env, err := fixture.Service(true).Plan(p)
	if err != nil {
		t.Fatal(err)
	}
	return env, env.Data.(connector.PlanResult)
}

func check(t *testing.T, items ...connector.CheckItem) (*connector.Envelope, []connector.CheckResultItem) {
	t.Helper()
	env, err := fixture.Service(true).Check(connector.CheckParams{Items: items})
	if err != nil {
		t.Fatal(err)
	}
	return env, env.Data.(connector.CheckResult).Items
}

func sourceNames(m connector.Meta) string {
	var names []string
	for _, src := range m.Sources {
		names = append(names, src.Name)
	}
	return strings.Join(names, ",")
}

// A plan defaults to three days from today with both activities, and every
// day carries the sun and moon at the point.
func TestPlanDefaults(t *testing.T) {
	env, res := plan(t, connector.PlanParams{Lat: f(57.191), Lon: f(-3.834)})
	q := res.Query
	if q.Lat != 57.19 || q.Lon != -3.83 || q.RadiusKm != connector.DefaultRadiusKm || q.From != "2026-10-19" || q.To != "2026-10-21" ||
		strings.Join(q.Activities, ",") != "walk,stargaze" || q.Limit != connector.DefaultOptions {
		t.Fatalf("query = %+v", q)
	}
	if len(res.Days) != 3 {
		t.Fatalf("days = %d", len(res.Days))
	}
	for _, d := range res.Days {
		if d.Sunrise == nil || d.Sunset == nil || *d.Sunrise >= *d.Sunset || d.MoonIllumination <= 0 || d.MoonIllumination > 1 {
			t.Errorf("%s sky = %v %v %v", d.Date, d.Sunrise, d.Sunset, d.MoonIllumination)
		}
	}
	if env.Meta.DataVersion != `"s1"|"h1"|"w1"` || env.Meta.ForecastFetchedAt == nil || *env.Meta.ForecastFetchedAt != "2026-10-19T14:05:00Z" {
		t.Fatalf("meta = %+v", env.Meta)
	}
	if !strings.Contains(sourceNames(env.Meta), "Walkhighlands") || !strings.Contains(sourceNames(env.Meta), "CERRA") || len(env.Meta.Notes) != 2 {
		t.Fatalf("meta = %+v", env.Meta)
	}
}

// Only options that fit come back, go before marginal; when none fit, the
// day says why instead.
func TestPlanVerdicts(t *testing.T) {
	env, res := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)})
	today, tomorrow, after := res.Days[0], res.Days[1], res.Days[2]
	if len(today.Walks) != 0 || !strings.Contains(today.WalkNote, "None of the 2 walks") {
		t.Errorf("today walks = %+v %q", today.Walks, today.WalkNote)
	}
	if len(tomorrow.Walks) != 1 || tomorrow.Walks[0].WalkID != "wh-meall-a-bhuachaille" || tomorrow.Walks[0].Verdict != verdict.Go ||
		tomorrow.Walks[0].StartBy == nil || *tomorrow.Walks[0].StartBy != "2026-10-20T11:00:00Z" || tomorrow.WalkNote != "" {
		t.Errorf("tomorrow walks = %+v", tomorrow.Walks)
	}
	// scotland-0002 lists 1 of its 30 clear hours, so tonight it cannot say
	// and is left out.
	if len(today.Stargazing) != 1 || today.Stargazing[0].SiteID != "scotland-0001" {
		t.Errorf("tonight = %+v", today.Stargazing)
	}
	for _, o := range today.Stargazing {
		if o.Verdict != verdict.Go && o.Verdict != verdict.Marginal {
			t.Errorf("tonight %s verdict %s", o.SiteID, o.Verdict)
		}
	}
	if len(after.Walks) != 0 || len(after.Stargazing) != 0 || !strings.Contains(after.StarNote, "Beyond the hourly forecast") || !strings.Contains(after.WalkNote, "hourly forecast reaches only") {
		t.Errorf("day 3 = %+v", after)
	}
	if env.Results != 1+1+1 {
		t.Errorf("results = %d", env.Results)
	}
}

func TestPlanFilters(t *testing.T) {
	env, res := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"stargaze"}, From: s("2026-10-20"), To: s("2026-10-20")})
	if len(res.Days) != 1 || res.Days[0].Walks != nil || res.Days[0].WalkNote != "" || len(res.Days[0].Stargazing) != 1 {
		t.Fatalf("stargaze only = %+v", res.Days)
	}
	if strings.Contains(sourceNames(env.Meta), "Walkhighlands") || len(env.Meta.Notes) != 1 || env.Meta.DataVersion != `"s1"|"h1"` {
		t.Fatalf("stargaze only meta = %+v", env.Meta)
	}
	env, res = plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, WalkFilter: connector.WalkFilter{MaxAscentM: i(500)}, From: s("2026-10-20"), To: s("2026-10-20")})
	if res.Days[0].Walks != nil || !strings.Contains(res.Days[0].WalkNote, "No walk starts within 30 km") || res.Days[0].Stargazing != nil {
		t.Fatalf("max ascent = %+v", res.Days[0])
	}
	if strings.Contains(sourceNames(env.Meta), "CERRA") {
		t.Fatalf("walk only meta = %+v", env.Meta)
	}
}

func TestPlanValidation(t *testing.T) {
	svc := fixture.Service(true)
	for param, p := range map[string]connector.PlanParams{
		"lat":            {Lon: f(-3.8)},
		"lat ":           {Lat: f(51.5), Lon: f(-0.12)},
		"radius_km":      {Lat: f(57), Lon: f(-3.8), RadiusKm: f(101)},
		"from":           {Lat: f(57), Lon: f(-3.8), From: s("2026-10-18")},
		"from ":          {Lat: f(57), Lon: f(-3.8), From: s("2026-11-02")},
		"to":             {Lat: f(57), Lon: f(-3.8), From: s("2026-10-20"), To: s("2026-10-27")},
		"to ":            {Lat: f(57), Lon: f(-3.8), From: s("2026-10-20"), To: s("2026-10-19")},
		"activities":     {Lat: f(57), Lon: f(-3.8), Activities: []string{"ski"}},
		"max_duration_h": {Lat: f(57), Lon: f(-3.8), WalkFilter: connector.WalkFilter{MaxDurationH: f(0)}},
		"max_ascent_m":   {Lat: f(57), Lon: f(-3.8), WalkFilter: connector.WalkFilter{MaxAscentM: i(3001)}},
		"limit":          {Lat: f(57), Lon: f(-3.8), Limit: i(connector.MaxOptions + 1)},
	} {
		_, err := svc.Plan(p)
		var pe *connector.ParamError
		if !errors.As(err, &pe) || pe.Param != strings.TrimSpace(param) {
			t.Errorf("%s: err = %v", param, err)
		}
	}
	// The span may reach the last day ahead, but no further.
	if _, err := svc.Plan(connector.PlanParams{Lat: f(57), Lon: f(-3.8), From: s("2026-11-01"), To: s("2026-11-01")}); err != nil {
		t.Errorf("last day: %v", err)
	}
	if _, err := svc.Plan(connector.PlanParams{Lat: f(57), Lon: f(-3.8), From: s(" "), To: s("")}); err != nil {
		t.Errorf("blank dates are absent: %v", err)
	}
}

func TestUnavailable(t *testing.T) {
	svc := fixture.Service(false)
	if _, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)}); !errors.Is(err, connector.ErrUnavailable) {
		t.Errorf("plan: %v", err)
	}
	if _, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.3), Lon: f(-3.7)}}}); !errors.Is(err, connector.ErrUnavailable) {
		t.Errorf("check: %v", err)
	}
}

// The walk snapshot is optional: without it a plan or check still answers
// its stargazing, and only a walks-only question is unavailable.
func TestWithoutWalks(t *testing.T) {
	svc := fixture.Service(true)
	svc.Walks = fixture.WalkSource{}
	env, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)})
	if err != nil {
		t.Fatal(err)
	}
	day := env.Data.(connector.PlanResult).Days[0]
	if day.Walks != nil || !strings.Contains(day.WalkNote, "not loaded yet") || len(day.Stargazing) == 0 || strings.Contains(sourceNames(env.Meta), "Walkhighlands") {
		t.Fatalf("plan without walks = %+v", day)
	}
	env, err = svc.Check(connector.CheckParams{Items: []connector.CheckItem{
		{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"},
		{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.3), Lon: f(-3.7)},
	}})
	if err != nil {
		t.Fatal(err)
	}
	items := env.Data.(connector.CheckResult).Items
	if !strings.Contains(items[0].Error, "not loaded yet") || items[1].Verdict != verdict.Marginal || env.Results != 1 {
		t.Fatalf("check without walks = %+v", items)
	}
	if _, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}}); !errors.Is(err, connector.ErrUnavailable) {
		t.Errorf("walk-only plan: %v", err)
	}
	if _, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"}}}); !errors.Is(err, connector.ErrUnavailable) {
		t.Errorf("walk-only check: %v", err)
	}
}

// Equal questions share an ETag; a different one does not.
func TestETags(t *testing.T) {
	a, _ := plan(t, connector.PlanParams{Lat: f(57.191), Lon: f(-3.834), Activities: []string{"stargaze", "walk"}})
	b, _ := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), To: s("2026-10-21")})
	c, _ := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Limit: i(2)})
	if a.ETag != b.ETag || a.ETag == c.ETag {
		t.Errorf("plan etags %s %s %s", a.ETag, b.ETag, c.ETag)
	}
	// check is a POST, which no cache keys on.
	if d, _ := check(t, connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"}); d.ETag != "" {
		t.Errorf("check etag %s", d.ETag)
	}
}

// A walk that does not fit says why and offers a nearby walk that does.
func TestCheckWalk(t *testing.T) {
	env, items := check(t,
		connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "Cairn Gorm"},
		connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkID: "wh-meall-a-bhuachaille"},
	)
	no, yes := items[0], items[1]
	if no.Verdict != verdict.No || !strings.Contains(no.Why, "longest good spell is 2 h") || no.Walk == nil || no.Walk.WalkID != "wh-cairn-gorm" || no.Walk.Slot != nil {
		t.Fatalf("cairn gorm = %+v", no)
	}
	// Meall a' Bhuachaille fits, but at 3 h it is no substitute for 9 h.
	if no.NearbyWalks != nil {
		t.Fatalf("cairn gorm nearby = %+v", no.NearbyWalks)
	}
	if yes.Verdict != verdict.Go || yes.Walk.Slot == nil || yes.Walk.StartFrom == nil || yes.NearbyWalks != nil || yes.Sky == nil || yes.Sky.Sunrise == nil {
		t.Fatalf("meall = %+v", yes)
	}
	if strings.Contains(sourceNames(env.Meta), "CERRA") || env.Results != 2 {
		t.Fatalf("meta %+v results %d", env.Meta, env.Results)
	}
}

// A better walk nearby is one of similar length that fits the same day.
func TestCheckNearbyWalksAreSubstitutes(t *testing.T) {
	hours := func(n int) string {
		var h []string
		for i := 0; i < n; i++ {
			h = append(h, fmt.Sprintf("[%d,7,0.0,15,30]", 1792483200+i*3600))
		}
		return strings.Join(h, ",")
	}
	body := `{"schema_version":1,"fetched_at":"2026-10-19T14:20:00+00:00","walks":[` +
		`{"id":"asked","name":"Asked","url":"https://www.walkhighlands.co.uk/a.shtml","lat":57.1,"lon":-3.7,"distance_km":12,"ascent_m":800,"duration_h":6,"hours":[` + hours(2) + `]},` +
		`{"id":"similar","name":"Similar","url":"https://www.walkhighlands.co.uk/s.shtml","lat":57.12,"lon":-3.7,"distance_km":11,"ascent_m":700,"duration_h":5,"hours":[` + hours(6) + `]},` +
		`{"id":"short","name":"Short","url":"https://www.walkhighlands.co.uk/b.shtml","lat":57.1,"lon":-3.71,"distance_km":3,"ascent_m":50,"duration_h":1,"hours":[` + hours(6) + `]},` +
		`{"id":"far","name":"Far","url":"https://www.walkhighlands.co.uk/f.shtml","lat":57.5,"lon":-3.7,"distance_km":12,"ascent_m":800,"duration_h":6,"hours":[` + hours(6) + `]}]}`
	wk, err := walks.Parse([]byte(body), "w")
	if err != nil {
		t.Fatal(err)
	}
	svc := fixture.Service(true)
	svc.Walks = fixture.WalkSource{DS: wk}
	env, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkID: "asked"}}})
	if err != nil {
		t.Fatal(err)
	}
	it := env.Data.(connector.CheckResult).Items[0]
	if it.Verdict != verdict.No || len(it.NearbyWalks) != 1 || it.NearbyWalks[0].WalkID != "similar" {
		t.Fatalf("nearby = %+v", it)
	}
}

// Before 09:00 the night under way is yesterday's: check accepts its date,
// and a plan with stargazing starts on it.
func TestTonightAfterMidnight(t *testing.T) {
	svc := fixture.Service(true)
	svc.Now = func() time.Time { return time.Date(2026, 10, 19, 23, 30, 0, 0, time.UTC) } // 00:30 BST on the 20th
	env, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.10), Lon: f(-3.90)}}})
	if err != nil {
		t.Fatal(err)
	}
	if it := env.Data.(connector.CheckResult).Items[0]; it.Error != "" || it.Night == nil || it.Night.SiteID != "scotland-0002" {
		t.Fatalf("tonight = %+v", it)
	}
	// A walk on that date is over.
	if _, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-19", WalkName: "gorm"}}}); err == nil {
		t.Fatal("a walk yesterday was accepted")
	}
	env, err = svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)})
	if err != nil {
		t.Fatal(err)
	}
	res := env.Data.(connector.PlanResult)
	// It still covers three days of walks: today and the two after.
	if res.Query.From != "2026-10-19" || res.Query.To != "2026-10-22" || res.Days[0].Walks != nil || !strings.Contains(res.Days[0].WalkNote, "This day is over") {
		t.Fatalf("plan = %+v", res.Query)
	}
	// A walk-only plan cannot start on a night it does not plan.
	if _, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, From: s("2026-10-19")}); err == nil {
		t.Fatal("a walk-only plan accepted yesterday")
	}
	env, err = svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}})
	if err != nil || env.Data.(connector.PlanResult).Query.From != "2026-10-20" {
		t.Fatalf("walk plan from = %v %v", env, err)
	}
}

// Windows ranks on listed hours, so a capped site whose list stops before
// the night can fall outside the top sites; the answer is still unknown.
func TestCappedSiteOutsideTheTopIsStillUnknown(t *testing.T) {
	var sites []string
	for i := 0; i < 60; i++ {
		sites = append(sites, fmt.Sprintf(`{"id":"near-%02d","lat":%.3f,"lon":-3.8,"lp_zone":"rural","clear_dark_hours":0,"clear_twilight_hours":0,"best_hours":[]}`, i, 57.0+float64(i)*0.001))
	}
	sites = append(sites, `{"id":"capped","lat":57.08,"lon":-3.8,"lp_zone":"rural","clear_dark_hours":30,"clear_twilight_hours":30,"best_hours":[{"time":"2026-10-19T22:00:00+00:00","cloud_area_fraction":1,"dark":true}]}`)
	sky, err := stars.Parse([]byte(`{"fetched_at":"2026-10-19T14:05:00+00:00","sites":[`+strings.Join(sites, ",")+`]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	svc := fixture.Service(true)
	svc.Data = fixture.Source{DS: sky}
	env, err := svc.Plan(connector.PlanParams{Lat: f(57.0), Lon: f(-3.8), Activities: []string{"stargaze"}, From: s("2026-10-20"), To: s("2026-10-20")})
	if err != nil {
		t.Fatal(err)
	}
	if note := env.Data.(connector.PlanResult).Days[0].StarNote; !strings.HasPrefix(note, "At site capped, 8.9 km away:") || !strings.Contains(note, "not counted yet") {
		t.Fatalf("plan note = %q", note)
	}
	env, err = svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "stargaze", Date: "2026-10-20", Lat: f(57.0), Lon: f(-3.8)}}})
	if err != nil {
		t.Fatal(err)
	}
	if it := env.Data.(connector.CheckResult).Items[0]; it.Verdict != verdict.Unknown || it.Night.SiteID != "capped" || it.Night.FromPointKm < 8.5 {
		t.Fatalf("check = %+v", it)
	}
}

// A plan filters walks by time, length and ascent, and checks a minimum
// against its maximum.
func TestPlanWalkFilters(t *testing.T) {
	day := connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, From: s("2026-10-20"), To: s("2026-10-20")}
	for name, c := range map[string]struct {
		f    connector.WalkFilter
		want int
	}{
		"none":         {connector.WalkFilter{}, 1},
		"long walks":   {connector.WalkFilter{MinDurationH: f(5)}, 0},
		"short walks":  {connector.WalkFilter{MaxDurationH: f(4)}, 1},
		"length range": {connector.WalkFilter{MinLengthKm: f(5), MaxLengthKm: f(10)}, 1},
		"too long":     {connector.WalkFilter{MinLengthKm: f(8)}, 0},
	} {
		p := day
		p.WalkFilter = c.f
		_, res := plan(t, p)
		if got := len(res.Days[0].Walks); got != c.want {
			t.Errorf("%s: %d walks %q", name, got, res.Days[0].WalkNote)
		}
	}
	p := day
	p.WalkFilter = connector.WalkFilter{MinDurationH: f(5), MaxDurationH: f(4)}
	var pe *connector.ParamError
	if _, err := fixture.Service(true).Plan(p); !errors.As(err, &pe) || pe.Param != "min_duration_h" {
		t.Fatalf("min above max: %v", err)
	}
	p.WalkFilter = connector.WalkFilter{MaxLengthKm: f(101)}
	if _, err := fixture.Service(true).Plan(p); !errors.As(err, &pe) || pe.Param != "max_length_km" {
		t.Fatalf("length cap: %v", err)
	}
}

// Weather limits tighten the good hours: a dry walk drops the drizzly
// hours, and the slot says how wet it is. check shows the slot hour by
// hour; plan stays compact.
func TestWeatherLimits(t *testing.T) {
	walk := connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkID: "wh-meall-a-bhuachaille"}
	_, items := check(t, walk)
	slot := items[0].Walk.Slot
	if items[0].Verdict != verdict.Go || slot.WetHours != 2 || slot.MaxRainMMH != 0.2 || len(slot.Hours) != 6 {
		t.Fatalf("default slot = %+v", slot)
	}
	if h := slot.Hours[1]; !h.Good || h.RainMM == nil || *h.RainMM != 0.1 || h.WindKmh == nil || h.TempC == nil || h.CloudPct == nil {
		t.Fatalf("hour = %+v", h)
	}
	// Dry only: 09:00Z (0.1 mm) and 12:00Z (0.2 mm) drop out, leaving 08:00Z,
	// 10:00Z and 11:00Z with one bad hour between: fits, but marginal.
	env, err := fixture.Service(true).Check(connector.CheckParams{Items: []connector.CheckItem{walk}, WeatherLimits: connector.WeatherLimits{MaxRainMMH: f(0)}})
	if err != nil {
		t.Fatal(err)
	}
	res := env.Data.(connector.CheckResult)
	dry := res.Items[0]
	// The hour turned away keeps its known weather, so an agent can see it
	// was drizzle, and the why names it.
	gap := dry.Walk.Slot.Hours[1]
	if dry.Verdict != verdict.Marginal || dry.Walk.Slot.WetHours != 0 || gap.Good || len(gap.Fails) != 1 || gap.Fails[0] != "rain" || gap.RainMM == nil || *gap.RainMM != 0.1 ||
		!strings.Contains(dry.Why, "one wet hour") || !strings.Contains(dry.Why, "otherwise dry") {
		t.Fatalf("dry = %+v %+v", dry, gap)
	}
	if res.Limits == nil || *res.Limits.MaxRainMMH != 0 {
		t.Fatalf("limits echo = %+v", res.Limits)
	}
	// Calm only: every hour is windier than 10 km/h.
	env, err = fixture.Service(true).Check(connector.CheckParams{Items: []connector.CheckItem{walk}, WeatherLimits: connector.WeatherLimits{MaxWindKmh: f(10)}})
	if err != nil {
		t.Fatal(err)
	}
	if calm := env.Data.(connector.CheckResult).Items[0]; calm.Verdict != verdict.No || !strings.Contains(calm.Why, "within the limits") {
		t.Fatalf("calm = %+v", calm)
	}
	// plan applies the same limits, echoes them, and leaves out the hours.
	pe, res2 := plan(t, connector.PlanParams{
		Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, From: s("2026-10-20"), To: s("2026-10-20"),
		WeatherLimits: connector.WeatherLimits{MaxRainMMH: f(0)},
	})
	if w := res2.Days[0].Walks; len(w) != 1 || w[0].Verdict != verdict.Marginal || w[0].Slot.Hours != nil || res2.Query.MaxRainMMH == nil {
		t.Fatalf("plan = %+v %+v", res2.Days[0], res2.Query)
	}
	if def, _ := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, From: s("2026-10-20"), To: s("2026-10-20")}); def.ETag == pe.ETag {
		t.Fatal("limits do not change the plan's ETag")
	}
	// Limits round to the data's 0.1 in their strict direction, never
	// admitting an hour the caller ruled out.
	for _, c := range []struct {
		in   connector.WeatherLimits
		want connector.WeatherLimits
	}{
		{connector.WeatherLimits{MaxRainMMH: f(0.05), MaxWindKmh: f(15.56), MinTempC: f(2.41)}, connector.WeatherLimits{MaxRainMMH: f(0), MaxWindKmh: f(15.5), MinTempC: f(2.5)}},
		{connector.WeatherLimits{MaxRainMMH: f(0.3), MinTempC: f(9.5)}, connector.WeatherLimits{MaxRainMMH: f(0.3), MinTempC: f(9.5)}},
		{connector.WeatherLimits{MinTempC: f(-0.05)}, connector.WeatherLimits{MinTempC: f(0)}},
	} {
		_, r := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), WeatherLimits: c.in})
		got, want := r.Query.WeatherLimits, c.want
		eq := func(a, b *float64) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }
		if !eq(got.MaxRainMMH, want.MaxRainMMH) || !eq(got.MaxWindKmh, want.MaxWindKmh) || !eq(got.MinTempC, want.MinTempC) {
			t.Errorf("limits %v rounded to %+v", c.in, got)
		}
	}
	_, negZero := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), WeatherLimits: connector.WeatherLimits{MinTempC: f(-0.05)}})
	if b, _ := json.Marshal(negZero.Query); !strings.Contains(string(b), `"min_temp_c":0`) {
		t.Errorf("-0.05 echoes as %s", b)
	}
	// A check of stargazing alone echoes no walk limits.
	if env, err := fixture.Service(true).Check(connector.CheckParams{
		Items:         []connector.CheckItem{{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.3), Lon: f(-3.7)}},
		WeatherLimits: connector.WeatherLimits{MaxRainMMH: f(0)},
	}); err != nil || env.Data.(connector.CheckResult).Limits != nil {
		t.Errorf("stargaze-only limits echo: %v", err)
	}
	for param, l := range map[string]connector.WeatherLimits{
		"max_rain_mm_h": {MaxRainMMH: f(10.5)},
		"max_wind_kmh":  {MaxWindKmh: f(-1)},
		"min_temp_c":    {MinTempC: f(31)},
	} {
		var perr *connector.ParamError
		if _, err := fixture.Service(true).Check(connector.CheckParams{Items: []connector.CheckItem{walk}, WeatherLimits: l}); !errors.As(err, &perr) || perr.Param != param {
			t.Errorf("check %s: %v", param, err)
		}
		if _, err := fixture.Service(true).Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), WeatherLimits: l}); !errors.As(err, &perr) || perr.Param != param {
			t.Errorf("plan %s: %v", param, err)
		}
	}
}

// v2Walks is a version 2 walk snapshot: one 3 h walk near Aviemore whose
// daylight on 20 October runs 08:00Z to 15:00Z, every hour present. 10:00Z
// and 11:00Z are wet (3 mm), 13:00Z is gusty, and the chance of rain is high
// from noon.
func v2Walks(t *testing.T) *walks.Dataset {
	t.Helper()
	var hours []string
	for i := 0; i < 8; i++ {
		rain, gust, chance := 0.0, 30, 10
		if i == 2 || i == 3 {
			rain = 3
		}
		if i == 5 {
			gust = 70
		}
		if i >= 4 {
			chance = 60
		}
		hours = append(hours, fmt.Sprintf("[%d,8,%g,20,50,%d,%g,%d,5,null,40]", 1792483200+i*3600, rain, gust, rain*1.5, chance))
	}
	body := `{"schema_version":2,"fetched_at":"2026-10-19T14:20:00+00:00","walks":[` +
		`{"id":"wet","name":"Wet Walk","url":"https://www.walkhighlands.co.uk/w.shtml","lat":57.17,"lon":-3.67,"distance_km":9,"ascent_m":400,"duration_h":3,"hours":[` + strings.Join(hours, ",") + `]}]}`
	ds, err := walks.Parse([]byte(body), "w2")
	if err != nil {
		t.Fatal(err)
	}
	return ds
}

// With every daylight hour in the data, limits loosen as well as tighten,
// gusts and the chance of rain can be limited, and a slot's hours carry their
// full weather and what a bad hour failed.
func TestV2WeatherLimits(t *testing.T) {
	svc := fixture.Service(true)
	svc.Walks = fixture.WalkSource{DS: v2Walks(t)}
	walk := connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkID: "wet"}
	run := func(l connector.WeatherLimits) connector.CheckResultItem {
		t.Helper()
		env, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{walk}, WeatherLimits: l})
		if err != nil {
			t.Fatal(err)
		}
		return env.Data.(connector.CheckResult).Items[0]
	}
	// Defaults: the 3 mm hours are bad, leaving 12:00Z to 16:00Z.
	def := run(connector.WeatherLimits{})
	if def.Verdict != verdict.Go || def.Walk.Slot.Start != "2026-10-20T12:00:00Z" || def.Walk.Slot.MaxGustKmh == nil || *def.Walk.Slot.MaxGustKmh != 70 ||
		def.Walk.Slot.MaxRainChancePct == nil || *def.Walk.Slot.MaxRainChancePct != 60 || def.Walk.Slot.MaxThunderChancePct == nil {
		t.Fatalf("default = %+v %+v", def, def.Walk.Slot)
	}
	h := def.Walk.Slot.Hours[0]
	if h.GustKmh == nil || *h.GustKmh != 30 || h.RainChancePct == nil || h.RainMaxMM == nil || h.LowCloudPct == nil || h.FogPct != nil || h.Fails != nil {
		t.Fatalf("hour = %+v", h)
	}
	// Loosened to 4 mm, the whole day is good from 08:00Z.
	if wet := run(connector.WeatherLimits{MaxRainMMH: f(4)}); wet.Verdict != verdict.Go || wet.Walk.Slot.Start != "2026-10-20T08:00:00Z" || wet.Walk.Slot.WetHours != 2 {
		t.Fatalf("loosened = %+v %+v", wet, wet.Walk.Slot)
	}
	// Gusts at most 50: 13:00Z is now the slot's one bad hour, named.
	g := run(connector.WeatherLimits{MaxGustKmh: f(50)})
	if g.Verdict != verdict.Marginal || !strings.Contains(g.Why, "one gusty hour") {
		t.Fatalf("gusts = %+v", g)
	}
	var bad *connector.SlotHour
	for i := range g.Walk.Slot.Hours {
		if !g.Walk.Slot.Hours[i].Good {
			bad = &g.Walk.Slot.Hours[i]
		}
	}
	if bad == nil || len(bad.Fails) != 1 || bad.Fails[0] != "gust" || bad.GustKmh == nil || *bad.GustKmh != 70 {
		t.Fatalf("bad hour = %+v", bad)
	}
	// At most a 50% chance of rain: from noon nothing is good.
	if c := run(connector.WeatherLimits{MaxRainChancePct: f(50)}); c.Verdict != verdict.No {
		t.Fatalf("rain chance = %+v", c)
	}
}

// On data with good hours only, gust and rain-chance limits cannot apply,
// and the answer says so rather than ignoring them.
func TestGustLimitsNeedTheFullData(t *testing.T) {
	var pe *connector.ParamError
	_, err := fixture.Service(true).Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"}},
		WeatherLimits: connector.WeatherLimits{MaxGustKmh: f(50)}})
	if !errors.As(err, &pe) || pe.Param != "max_gust_kmh" || !strings.Contains(pe.Message, "not available yet") {
		t.Fatalf("check: %v", err)
	}
	_, err = fixture.Service(true).Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), WeatherLimits: connector.WeatherLimits{MaxRainChancePct: f(30)}})
	if !errors.As(err, &pe) || pe.Param != "max_rain_chance_pct" {
		t.Fatalf("plan: %v", err)
	}
	// Stargazing alone does not need walk data, so the limit is moot there.
	if _, err := fixture.Service(true).Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"stargaze"}, WeatherLimits: connector.WeatherLimits{MaxGustKmh: f(50)}}); err != nil {
		t.Fatalf("stargaze plan: %v", err)
	}
}

// An item that cannot be resolved is an item error; the rest still answer.
func TestCheckItemErrors(t *testing.T) {
	env, items := check(t,
		connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "Ben Nevis"},
		connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkID: "wh-nope"},
		connector.CheckItem{Activity: "stargaze", Date: "2026-10-19", Lat: f(55.95), Lon: f(-3.19)},
		connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"},
	)
	for _, it := range items[:3] {
		if it.Error == "" || it.Verdict != "" || it.Walk != nil || it.Night != nil {
			t.Errorf("item = %+v", it)
		}
	}
	if items[3].Error != "" || items[3].Verdict != verdict.No {
		t.Errorf("good item = %+v", items[3])
	}
	if env.Results != 1 {
		t.Errorf("results = %d", env.Results)
	}
}

func TestCheckNight(t *testing.T) {
	_, items := check(t,
		connector.CheckItem{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.3), Lon: f(-3.7)},
		connector.CheckItem{Activity: "stargaze", Date: "2026-10-19", Lat: f(57.45), Lon: f(-3.7)},
	)
	at, far := items[0], items[1]
	if at.Verdict != verdict.Marginal || at.Night == nil || at.Night.SiteID != "scotland-0001" || at.Night.FromPointKm != 0 || at.Night.SiteNote != "" {
		t.Fatalf("at site = %+v", at)
	}
	if at.Night.ClearFrom == nil || *at.Night.ClearFrom != "2026-10-19T20:00:00Z" || at.Night.ClearSkyOdds == nil || *at.Night.ClearSkyOdds != 0.25 {
		t.Fatalf("at site night = %+v", at.Night)
	}
	if len(at.OtherDays) != 1 || at.OtherDays[0].Date != "2026-10-20" {
		t.Fatalf("other days = %+v", at.OtherDays)
	}
	if far.Night == nil || !strings.Contains(far.Night.SiteNote, "No dark-sky site within 10 km") || far.Night.FromPointKm < 10 {
		t.Fatalf("far = %+v", far)
	}
}

func TestCheckValidation(t *testing.T) {
	svc := fixture.Service(true)
	walk := func(it connector.CheckItem) connector.CheckParams {
		return connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkName: "gorm"}, it}}
	}
	for param, p := range map[string]connector.CheckParams{
		"items":                {},
		"items ":               {Items: make([]connector.CheckItem, connector.MaxCheckItems+1)},
		"items[1].activity":    walk(connector.CheckItem{Activity: "ski", Date: "2026-10-20"}),
		"items[1].date":        walk(connector.CheckItem{Activity: "walk", Date: "tomorrow", WalkName: "gorm"}),
		"items[1].date ":       walk(connector.CheckItem{Activity: "walk", Date: "2026-11-02", WalkName: "gorm"}),
		"items[1].walk_name":   walk(connector.CheckItem{Activity: "walk", Date: "2026-10-20"}),
		"items[1].walk_name ":  walk(connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "--"}),
		"items[1].walk_name  ": walk(connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: strings.Repeat("é", connector.MaxNameLength+1)}),
		"items[1].lat":         walk(connector.CheckItem{Activity: "stargaze", Date: "2026-10-20"}),
		"items[1].lat ":        walk(connector.CheckItem{Activity: "stargaze", Date: "2026-10-20", Lat: f(51.5), Lon: f(-0.12)}),
	} {
		_, err := svc.Check(p)
		var pe *connector.ParamError
		if !errors.As(err, &pe) || pe.Param != strings.TrimSpace(param) {
			t.Errorf("%q: err = %v", param, err)
		}
	}
	// A name of exactly the cap, in runes, is allowed.
	if _, err := svc.Check(walk(connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: strings.Repeat("é", connector.MaxNameLength)})); err != nil {
		t.Errorf("name at the cap: %v", err)
	}
}

// The wire shape: field names agents read, and verdicts as plain strings.
func TestWireShape(t *testing.T) {
	_, res := plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), From: s("2026-10-20"), To: s("2026-10-20")})
	b, err := json.Marshal(res.Days[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{
		`"date":"2026-10-20"`, `"sunrise":`, `"sunset":`, `"moon_illumination":`, `"walks":[`, `"verdict":"go"`, `"why":`,
		`"start_from":`, `"start_by":`, `"slot":{`, `"url":"https://www.walkhighlands.co.uk/`, `"stargazing":[`, `"clear_sky_odds":`,
		`"clear_from":`, `"moonless_clear_dark_hours":`, `"light_pollution_zone":`,
	} {
		if !strings.Contains(string(b), k) {
			t.Errorf("day lacks %s: %s", k, b)
		}
	}
}
