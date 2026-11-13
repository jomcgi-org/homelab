package connector

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

func TestWarningIntervalsIncludeDSTAndLongWalks(t *testing.T) {
	for _, tc := range []struct {
		date  string
		night bool
		hours int
	}{
		{"2026-03-29", false, 23},
		{"2026-10-25", false, 25},
		{"2026-03-28", true, 23},
		{"2026-10-24", true, 25},
	} {
		d, _ := time.Parse("2006-01-02", tc.date)
		day := int32(d.Unix() / 86400)
		from, to := warningBounds(day, tc.night)
		if to.Sub(from) != time.Duration(tc.hours)*time.Hour {
			t.Fatal(tc, from, to)
		}
	}
	now := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	snap := &warnings.Snapshot{Version: 1, CreatedAt: now}
	for _, code := range warnings.Regions {
		snap.Regions = append(snap.Regions, warnings.Region{Code: code, Complete: true, CheckedAt: now, Items: []warnings.Item{}})
	}
	title := "Amber warning of wind affecting Grampian"
	snap.Regions[2].Items = []warnings.Item{{Title: title, Description: title + ": Moray valid from 0300 Sun 25 Oct to 0400 Sun 25 Oct", Link: warnings.OfficialURL + "#?date=2026-10-25&id=synthetic"}}
	screen := &warningScreen{snapshot: snap, now: now}
	day := int32(now.Unix() / 86400)
	v, _ := screen.apply(day, false, verdict.Go, "")
	if v != verdict.Go {
		t.Fatal("warning outside normal walking date", v)
	}
	start := time.Date(2026, 10, 24, 8, 0, 0, 0, time.UTC)
	v, why := screen.walk(day, &walks.Walk{DurationH: 24}, walks.Assessment{Verdict: verdict.Go, StartFrom: start, StartBy: start})
	if v != verdict.No {
		t.Fatal("overnight walk escaped next-day warning", v, why)
	}
	v, _ = screen.apply(day, true, verdict.Go, "")
	if v != verdict.No {
		t.Fatal("overnight night missed warning")
	}
}
