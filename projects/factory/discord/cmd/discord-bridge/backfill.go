package main

import (
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"golang.org/x/time/rate"

	"github.com/jomcgi/homelab/projects/factory/discord"
)

// newBackfill builds the history backfill (HOME-836) from the environment,
// or returns nil when BACKFILL_ENABLED is not "true". BACKFILL_INTERVAL is
// the wait between passes. BACKFILL_RATE bounds the backfill's Discord reads
// and Hindsight calls per second, which it shares with live turns and
// ingest.
func newBackfill(log *slog.Logger, policy discord.Config, rest *discord.REST, hindsightURL string, key func() (string, error), reg prometheus.Registerer) (*discord.Backfill, error) {
	if os.Getenv("BACKFILL_ENABLED") != "true" {
		return nil, nil
	}
	interval := discord.DefaultBackfillInterval
	if v := os.Getenv("BACKFILL_INTERVAL"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			return nil, fmt.Errorf("BACKFILL_INTERVAL %q is not a positive duration", v)
		}
		interval = d
	}
	perSecond := 2.0
	if v := os.Getenv("BACKFILL_RATE"); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil || f <= 0 {
			return nil, fmt.Errorf("BACKFILL_RATE %q is not a positive number", v)
		}
		perSecond = f
	}
	limiter := rate.NewLimiter(rate.Limit(perSecond), 1)
	return &discord.Backfill{
		Importer: &discord.Importer{
			Source: rest,
			// A synchronous retain runs the LLM's extraction, which retries
			// on its own; one call took over 3 minutes on 2026-11-25.
			Store: &discord.Hindsight{
				BaseURL: strings.TrimSuffix(hindsightURL, "/"), Key: key,
				HTTP: &http.Client{Timeout: 10 * time.Minute},
			},
			Config:      policy,
			IncludeBots: os.Getenv("BACKFILL_INCLUDE_BOTS") == "true",
			// One channel at a time and no document cap: a pass holds at most
			// one retain, and the limiter spaces every call.
			Workers:       1,
			RetainRetries: 4,
			Backoff:       30 * time.Second,
			Pace:          limiter.Wait,
			SkipIngested:  true,
			Log:           log,
		},
		Interval: interval,
		Metrics:  discord.NewBackfillMetrics(reg),
		Log:      log,
	}, nil
}
