package discord

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// failingHistory fails a server's channel list while fail is set.
type failingHistory struct {
	*fakeHistory
	mu   sync.Mutex
	fail map[string]bool
}

func (f *failingHistory) GuildChannels(ctx context.Context, g string) ([]Channel, error) {
	f.mu.Lock()
	bad := f.fail[g]
	f.mu.Unlock()
	if bad {
		return nil, errors.New("discord: HTTP 500")
	}
	return f.fakeHistory.GuildChannels(ctx, g)
}

func (f *failingHistory) set(g string, bad bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fail[g] = bad
}

// clock is the Backfill's fake wait: each call to After reports the end of
// a pass on waits, and the next pass starts on a send to ticks.
type clock struct {
	waits chan time.Duration
	ticks chan time.Time
}

func newClock() *clock {
	return &clock{waits: make(chan time.Duration, 1), ticks: make(chan time.Time)}
}

func (c *clock) After(d time.Duration) <-chan time.Time {
	c.waits <- d
	return c.ticks
}

func (c *clock) passEnded(t *testing.T) time.Duration {
	t.Helper()
	select {
	case d := <-c.waits:
		return d
	case <-time.After(5 * time.Second):
		t.Fatal("no backfill pass ended")
		return 0
	}
}

type backfillRig struct {
	src     *failingHistory
	docs    *fakeDocs
	metrics *BackfillMetrics
	clock   *clock
	b       *Backfill
	ready   chan struct{}
	done    chan struct{}
	cancel  context.CancelFunc
}

func newBackfillRig(t *testing.T, guilds map[string]Guild) *backfillRig {
	t.Helper()
	src := history()
	src.messages["c9"] = append(src.messages["c9"], msg("x0", "dee", day(13, 12), "older"))
	r := &backfillRig{
		src:     &failingHistory{fakeHistory: src, fail: map[string]bool{}},
		docs:    &fakeDocs{},
		metrics: NewBackfillMetrics(prometheus.NewRegistry()),
		clock:   newClock(),
		ready:   make(chan struct{}),
		done:    make(chan struct{}),
	}
	im := newImporter(src, r.docs, guilds)
	im.Source = r.src
	im.Workers = 1
	im.SkipIngested = true
	r.b = &Backfill{
		Importer: im, Interval: 3 * time.Hour, Metrics: r.metrics,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), After: r.clock.After,
	}
	ctx, cancel := context.WithCancel(context.Background())
	r.cancel = cancel
	go func() {
		defer close(r.done)
		r.b.Run(ctx, r.ready)
	}()
	t.Cleanup(func() {
		cancel()
		<-r.done
	})
	return r
}

func (r *backfillRig) count(vec *prometheus.CounterVec, labels ...string) int {
	return int(testutil.ToFloat64(vec.WithLabelValues(labels...)))
}

func TestBackfillRunsAfterReadyThenEachInterval(t *testing.T) {
	r := newBackfillRig(t, map[string]Guild{"g1": {History: true}, "g2": {History: true}})
	// Nothing runs before the gateway is ready.
	time.Sleep(20 * time.Millisecond)
	if ids := r.docs.ids(); len(ids) != 0 {
		t.Fatalf("documents before ready: %v", ids)
	}
	close(r.ready)
	if d := r.clock.passEnded(t); d != 3*time.Hour {
		t.Errorf("wait after a pass = %v, want the interval", d)
	}
	want := []string{
		"g1/discord:c1:2026-11-15:history", "g1/discord:c1:2026-11-16:history", "g1/discord:t1:2026-11-14:history",
		"g2/discord:c9:2026-11-13:history", "g2/discord:c9:2026-11-14:history",
	}
	if got := r.docs.ids(); !slices.Equal(got, want) {
		t.Fatalf("documents = %v, want %v", got, want)
	}
	if r.count(r.metrics.Documents, "g1", "retained") != 3 || r.count(r.metrics.Passes, "success") != 1 {
		t.Errorf("first pass metrics: g1 retained %d, passes %d",
			r.count(r.metrics.Documents, "g1", "retained"), r.count(r.metrics.Passes, "success"))
	}
	if v := testutil.ToFloat64(r.metrics.CaughtUp.WithLabelValues("g1")); v != 0 {
		t.Errorf("caught up after a pass that retained = %v, want 0", v)
	}
	if testutil.ToFloat64(r.metrics.LastFinished) == 0 {
		t.Error("last pass finish time not set")
	}

	// A new day finished: the next pass stores only it and skips the rest.
	r.src.messages["c1"] = append([]HistoryMessage{msg("m10", "ann", day(18, 8), "today")}, r.src.messages["c1"]...)
	r.b.Importer.Now = func() time.Time { return day(18, 21) }
	r.clock.ticks <- time.Time{}
	r.clock.passEnded(t)
	if !slices.Contains(r.docs.ids(), "g1/discord:c1:2026-11-17:history") || len(r.docs.ids()) != 6 {
		t.Fatalf("second pass documents = %v", r.docs.ids())
	}
	if got := r.count(r.metrics.Documents, "g1", "skipped"); got != 3 {
		t.Errorf("g1 skipped = %d, want the 3 stored days", got)
	}
	if v := testutil.ToFloat64(r.metrics.CaughtUp.WithLabelValues("g2")); v != 1 {
		t.Errorf("g2 caught up = %v, want 1: its pass retained nothing", v)
	}

	// Cancel stops the loop while it waits.
	r.cancel()
	select {
	case <-r.done:
	case <-time.After(5 * time.Second):
		t.Fatal("backfill did not stop on cancel")
	}
}

func TestBackfillSurvivesAFailingServer(t *testing.T) {
	r := newBackfillRig(t, map[string]Guild{"g1": {History: true}, "g2": {History: true}})
	r.src.set("g1", true)
	close(r.ready)
	r.clock.passEnded(t)
	// g2 still imports in the failed pass.
	if got := r.docs.ids(); !slices.Equal(got, []string{"g2/discord:c9:2026-11-13:history", "g2/discord:c9:2026-11-14:history"}) {
		t.Fatalf("documents after a failed server = %v", got)
	}
	if r.count(r.metrics.Passes, "failure") != 1 {
		t.Errorf("failed passes = %d, want 1", r.count(r.metrics.Passes, "failure"))
	}
	r.src.set("g1", false)
	r.clock.ticks <- time.Time{}
	r.clock.passEnded(t)
	if len(r.docs.ids()) != 5 || r.count(r.metrics.Passes, "success") != 1 {
		t.Fatalf("retry pass: documents %v, successes %d", r.docs.ids(), r.count(r.metrics.Passes, "success"))
	}
}

func TestBackfillSkipsDaysTheIngestorStored(t *testing.T) {
	r := newBackfillRig(t, map[string]Guild{"g1": {History: true, Ingest: true}, "g2": {History: true}})
	// Live ingest stored c1's 2026-11-16 in g1, and a tagged exchange in g2
	// (no ingest) on 2026-11-14.
	_ = r.docs.RetainDocument(context.Background(), "g1", NewRetain("c1", day(16, 10), "ann: ingested"))
	_ = r.docs.RetainDocument(context.Background(), "g2", NewRetain("c9", day(14, 10), "dee: tagged"))
	close(r.ready)
	r.clock.passEnded(t)
	ids := r.docs.ids()
	if slices.Contains(ids, "g1/discord:c1:2026-11-16:history") {
		t.Errorf("an ingested day was imported again: %v", ids)
	}
	if !slices.Contains(ids, "g2/discord:c9:2026-11-14:history") {
		t.Errorf("a server without ingest lost its day: %v", ids)
	}
}

// Every Discord read and Hindsight call waits for Pace first.
func TestBackfillPacesEveryCall(t *testing.T) {
	src := history()
	docs := &countingDocs{}
	im := newImporter(src, nil, map[string]Guild{"g1": {History: true}})
	im.Store = docs
	im.Workers = 1
	counted := &countingHistory{fakeHistory: src}
	im.Source = counted
	var paces int
	im.Pace = func(ctx context.Context) error {
		paces++
		return ctx.Err()
	}
	if _, err := im.RunGuild(context.Background(), "g1"); err != nil {
		t.Fatal(err)
	}
	calls := counted.calls + docs.calls
	if calls == 0 || paces != calls {
		t.Errorf("paces = %d, calls = %d: each call needs one pace", paces, calls)
	}
	// A pace that fails (the pass is cancelled) stops the run.
	im.Pace = func(context.Context) error { return context.Canceled }
	if _, err := im.RunGuild(context.Background(), "g1"); !errors.Is(err, context.Canceled) {
		t.Errorf("run with a failing pace = %v", err)
	}
	if _, err := im.RunGuild(context.Background(), "g9"); err == nil {
		t.Error("a server without history ran")
	}
}

type countingHistory struct {
	*fakeHistory
	calls int
}

func (c *countingHistory) GuildChannels(ctx context.Context, g string) ([]Channel, error) {
	c.calls++
	return c.fakeHistory.GuildChannels(ctx, g)
}

func (c *countingHistory) Threads(ctx context.Context, g string, ch []Channel) ([]Channel, error) {
	c.calls++
	return c.fakeHistory.Threads(ctx, g, ch)
}

func (c *countingHistory) MessagesBefore(ctx context.Context, ch, before string) ([]HistoryMessage, error) {
	c.calls++
	return c.fakeHistory.MessagesBefore(ctx, ch, before)
}

type countingDocs struct {
	fakeDocs
	calls int
}

func (c *countingDocs) DocumentExists(ctx context.Context, g, id string) (bool, error) {
	c.calls++
	return c.fakeDocs.DocumentExists(ctx, g, id)
}

func (c *countingDocs) RetainDocument(ctx context.Context, g string, item RetainItem) error {
	c.calls++
	return c.fakeDocs.RetainDocument(ctx, g, item)
}
