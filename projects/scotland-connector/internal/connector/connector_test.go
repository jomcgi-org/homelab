package connector_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
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
	env, res = plan(t, connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), Activities: []string{"walk"}, MaxAscentM: i(500), From: s("2026-10-20"), To: s("2026-10-20")})
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
		"max_duration_h": {Lat: f(57), Lon: f(-3.8), MaxDurationH: f(0)},
		"max_ascent_m":   {Lat: f(57), Lon: f(-3.8), MaxAscentM: i(3001)},
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
	d, _ := check(t, connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "Cairn  Gorm"})
	e, _ := check(t, connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: "cairn gorm"})
	g, _ := check(t, connector.CheckItem{Activity: "walk", Date: "2026-10-21", WalkName: "cairn gorm"})
	if d.ETag != e.ETag || d.ETag == g.ETag || d.ETag == a.ETag {
		t.Errorf("check etags %s %s %s", d.ETag, e.ETag, g.ETag)
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
	if len(no.NearbyWalks) != 1 || no.NearbyWalks[0].WalkID != "wh-meall-a-bhuachaille" || no.NearbyWalks[0].Verdict != verdict.Go || no.NearbyWalks[0].FromPointKm == nil {
		t.Fatalf("cairn gorm nearby = %+v", no.NearbyWalks)
	}
	if yes.Verdict != verdict.Go || yes.Walk.Slot == nil || yes.Walk.StartFrom == nil || yes.NearbyWalks != nil || yes.Sky == nil || yes.Sky.Sunrise == nil {
		t.Fatalf("meall = %+v", yes)
	}
	if strings.Contains(sourceNames(env.Meta), "CERRA") || env.Results != 2 {
		t.Fatalf("meta %+v results %d", env.Meta, env.Results)
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
	for _, k := range []string{`"date":"2026-10-20"`, `"sunrise":`, `"sunset":`, `"moon_illumination":`, `"walks":[`, `"verdict":"go"`, `"why":`,
		`"start_from":`, `"start_by":`, `"slot":{`, `"url":"https://www.walkhighlands.co.uk/`, `"stargazing":[`, `"clear_sky_odds":`,
		`"clear_from":`, `"moonless_clear_dark_hours":`, `"light_pollution_zone":`} {
		if !strings.Contains(string(b), k) {
			t.Errorf("day lacks %s: %s", k, b)
		}
	}
}
