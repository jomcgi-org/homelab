package main

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/discord"
)

func TestNewBackfillReadsTheEnvironment(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	build := func() (*discord.Backfill, error) {
		return newBackfill(log, discord.Config{}, &discord.REST{}, "http://h/", nil, nil)
	}
	if b, err := build(); b != nil || err != nil {
		t.Fatalf("off by default: %v, %v", b, err)
	}
	t.Setenv("BACKFILL_ENABLED", "true")
	b, err := build()
	if err != nil || b.Interval != discord.DefaultBackfillInterval || b.Importer.Workers != 1 ||
		b.Importer.Pace == nil || !b.Importer.SkipIngested || b.Importer.IncludeBots || b.Importer.MaxDocuments != 0 {
		t.Fatalf("defaults = %+v, %v", b, err)
	}
	t.Setenv("BACKFILL_INTERVAL", "90m")
	t.Setenv("BACKFILL_RATE", "0.5")
	t.Setenv("BACKFILL_INCLUDE_BOTS", "true")
	if b, err = build(); err != nil || b.Interval != 90*time.Minute || !b.Importer.IncludeBots {
		t.Fatalf("set values = %+v, %v", b, err)
	}
	for name, bad := range map[string]string{"BACKFILL_INTERVAL": "0s", "BACKFILL_RATE": "-1"} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(name, bad)
			if _, err := build(); err == nil {
				t.Errorf("%s=%s accepted", name, bad)
			}
		})
	}
}
