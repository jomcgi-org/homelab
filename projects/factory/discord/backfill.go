package discord

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// DefaultBackfillInterval is the wait between the end of one backfill pass
// and the start of the next.
const DefaultBackfillInterval = 6 * time.Hour

// Backfill runs the history Importer inside the bridge (HOME-836): one pass
// after the gateway is ready, then one pass each Interval after the last
// pass ends. A pass imports each server with `history: true` in turn. Stored
// channel-days are skipped and today is left to live ingest, so a pass after
// the first adds only the days that finished since. A failed server or pass
// logs and waits for the next pass; it never stops the bridge.
type Backfill struct {
	// Importer does the work. Its Workers, Pace and SkipIngested set the
	// pass's priority; Observe is the Backfill's own.
	Importer *Importer
	Interval time.Duration
	Metrics  *BackfillMetrics
	Log      *slog.Logger
	// After waits between passes; time.After when nil (tests replace it).
	After func(time.Duration) <-chan time.Time
	Now   func() time.Time
}

// Run waits for ready, then runs passes until ctx ends.
func (b *Backfill) Run(ctx context.Context, ready <-chan struct{}) {
	select {
	case <-ctx.Done():
		return
	case <-ready:
	}
	if b.Metrics != nil {
		b.Importer.Observe = b.Metrics.observe
	}
	for {
		b.pass(ctx)
		select {
		case <-ctx.Done():
			return
		case <-b.after(b.interval()):
		}
	}
}

// pass imports each history server once. A panic ends the pass, not the
// bridge.
func (b *Backfill) pass(ctx context.Context) {
	failed := false
	defer func() {
		if r := recover(); r != nil {
			b.Log.Error("history backfill panicked", "panic", fmt.Sprint(r))
			failed = true
		}
		if ctx.Err() != nil {
			return // Shutdown: the pass did not finish.
		}
		if b.Metrics != nil {
			b.Metrics.finish(failed, b.now())
		}
	}()
	start := b.now()
	var total ImportStats
	for _, gid := range b.Importer.Config.HistoryGuilds() {
		stats, err := b.Importer.RunGuild(ctx, gid)
		total.Channels += stats.Channels
		total.Retained += stats.Retained
		total.Skipped += stats.Skipped
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			failed = true
			b.Log.Warn("history backfill failed for a server; the next pass tries again",
				"guild", gid, "retained", stats.Retained, "skipped", stats.Skipped, "error", err)
			continue
		}
		if b.Metrics != nil {
			b.Metrics.caughtUp(gid, stats.Retained == 0)
		}
	}
	b.Log.Info("history backfill pass finished", "channels", total.Channels, "retained", total.Retained,
		"skipped", total.Skipped, "failed", failed, "duration", b.now().Sub(start).String(),
		"next", b.interval().String())
}

func (b *Backfill) interval() time.Duration {
	if b.Interval > 0 {
		return b.Interval
	}
	return DefaultBackfillInterval
}

func (b *Backfill) after(d time.Duration) <-chan time.Time {
	if b.After != nil {
		return b.After(d)
	}
	return time.After(d)
}

func (b *Backfill) now() time.Time {
	if b.Now != nil {
		return b.Now()
	}
	return time.Now()
}

// BackfillMetrics is the backfill's Prometheus metrics. The guild label
// takes only allowlisted server IDs, from the policy.
type BackfillMetrics struct {
	Documents    *prometheus.CounterVec // guild, outcome
	Passes       *prometheus.CounterVec // result
	LastFinished prometheus.Gauge
	CaughtUp     *prometheus.GaugeVec // guild
}

// NewBackfillMetrics registers the metrics with reg; a nil reg leaves them
// unregistered.
func NewBackfillMetrics(reg prometheus.Registerer) *BackfillMetrics {
	m := &BackfillMetrics{
		Documents: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_discord_backfill_documents_total",
			Help: "History channel-days the backfill read, by guild and outcome (retained: stored now; skipped: stored before or ingested live).",
		}, []string{"guild", "outcome"}),
		Passes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_discord_backfill_passes_total",
			Help: "Backfill passes that ended, by result (success, failure). A failure is a pass where a server failed.",
		}, []string{"result"}),
		LastFinished: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_discord_backfill_last_pass_finished_timestamp_seconds",
			Help: "Unix time the last backfill pass ended, with any result.",
		}),
		CaughtUp: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "factory_discord_backfill_caught_up",
			Help: "1 when the guild's last successful backfill retained no document: its history before today is stored.",
		}, []string{"guild"}),
	}
	if reg != nil {
		reg.MustRegister(m.Documents, m.Passes, m.LastFinished, m.CaughtUp)
	}
	return m
}

func (m *BackfillMetrics) observe(guildID string, retained bool) {
	outcome := "skipped"
	if retained {
		outcome = "retained"
	}
	m.Documents.WithLabelValues(guildID, outcome).Inc()
}

func (m *BackfillMetrics) caughtUp(guildID string, done bool) {
	v := 0.0
	if done {
		v = 1
	}
	m.CaughtUp.WithLabelValues(guildID).Set(v)
}

func (m *BackfillMetrics) finish(failed bool, at time.Time) {
	result := "success"
	if failed {
		result = "failure"
	}
	m.Passes.WithLabelValues(result).Inc()
	m.LastFinished.Set(float64(at.Unix()))
}
