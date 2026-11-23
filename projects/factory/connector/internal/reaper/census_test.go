package reaper

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"google.golang.org/protobuf/encoding/protowire"
)

func pbBytes(n protowire.Number, value []byte) []byte {
	return protowire.AppendBytes(protowire.AppendTag(nil, n, protowire.BytesType), value)
}

func pbVarint(n protowire.Number, value uint64) []byte {
	return protowire.AppendVarint(protowire.AppendTag(nil, n, protowire.VarintType), value)
}

// actorIn encodes an Actor with the given state on the named worker. An empty
// pod leaves the worker assignment out.
func actorIn(namespace, pool, pod string, state uint64) []byte {
	var worker []byte
	if pod != "" {
		worker = append(pbBytes(1, []byte(namespace)), pbBytes(2, []byte(pool))...)
		worker = append(worker, pbBytes(3, []byte(pod))...)
		worker = append(worker, pbBytes(4, []byte("uid-"+pod))...)
	}
	status := pbVarint(1, state)
	if worker != nil {
		status = append(status, pbBytes(2, worker)...)
	}
	return pbBytes(5, status)
}

func TestBusyWorkersCountsPodsThatHostAnActor(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	got, err := BusyWorkers([][]byte{
		actorIn(coder.Namespace, coder.Name, "homelab-codex-a", stateRunning),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-a", stateRunning), // same pod counts once
		actorIn(coder.Namespace, coder.Name, "homelab-codex-b", stateResuming),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-c", stateSuspending),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-d", stateReverting),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-e", 4), // SUSPENDED: no worker
		actorIn(coder.Namespace, coder.Name, "homelab-codex-f", 5), // CRASHED
		actorIn(coder.Namespace, coder.Name, "homelab-codex-g", 6), // DELETING
		actorIn("", "", "", 4), // SUSPENDED, no assignment: fine
		actorIn(review.Namespace, review.Name, "homelab-reviewer-a", stateRunning),
	})
	if err != nil {
		t.Fatal(err)
	}
	if got[coder] != 4 || got[review] != 1 || len(got) != 2 {
		t.Fatalf("busy workers = %v, want coder 4 and reviewer 1", got)
	}
}

// A row that cannot be read with certainty fails the whole census. Counting it
// as "no worker" would publish a low demand under a live actor.
func TestBusyWorkersRefusesAnyRowItCannotRead(t *testing.T) {
	worker := func(fields ...[]byte) []byte {
		var w []byte
		for _, f := range fields {
			w = append(w, f...)
		}
		return pbBytes(5, append(pbVarint(1, stateRunning), pbBytes(2, w)...))
	}
	ns, pool, pod := pbBytes(1, []byte("ns")), pbBytes(2, []byte("pool")), pbBytes(3, []byte("pod"))
	for name, row := range map[string][]byte{
		"not protobuf":                 {0xff, 0xff},
		"empty row":                    nil,
		"no status":                    pbBytes(1, []byte("metadata")),
		"truncated status":             {0x2a, 0x05, 0x08},
		"no state":                     pbBytes(5, pbBytes(2, []byte("x"))),
		"state zero":                   pbBytes(5, pbVarint(1, 0)),
		"state eight":                  pbBytes(5, pbVarint(1, 8)),
		"holding state, no assignment": pbBytes(5, pbVarint(1, stateRunning)),
		"holding state, empty pod":     worker(ns, pool, pbBytes(3, nil)),
		"holding state, no pod":        worker(ns, pool),
		"holding state, no pool":       worker(ns, pod),
		"holding state, no namespace":  worker(pool, pod),
		"malformed assignment":         pbBytes(5, append(pbVarint(1, stateRunning), pbBytes(2, []byte{0xff})...)),
	} {
		good := actorIn("ns", "pool", "pod", stateRunning)
		got, err := BusyWorkers([][]byte{good, row})
		if !errors.Is(err, ErrCensus) || got != nil {
			t.Fatalf("%s: got %v, %v; want ErrCensus and no counts", name, got, err)
		}
	}
	if _, err := BusyWorkers([][]byte{worker(ns, pool, pod)}); err != nil {
		t.Fatalf("a complete row: %v", err)
	}
}

// Census reads the table the reaper's database role may call and counts it.
// censusDB opens a fresh migrated database.
func censusDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func TestCensusReadsTheActorTable(t *testing.T) {
	db := censusDB(t)
	if _, err := db.Exec(`CREATE TABLE actors (name text, proto bytea);
		CREATE FUNCTION public.reaper_actors() RETURNS TABLE(name text, proto bytea)
		LANGUAGE sql STABLE AS $$SELECT a.name, a.proto FROM actors a$$`); err != nil {
		t.Fatal(err)
	}
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	for i, row := range [][]byte{
		actorIn(coder.Namespace, coder.Name, "a", stateRunning),
		actorIn(coder.Namespace, coder.Name, "b", stateSuspending),
		actorIn(coder.Namespace, coder.Name, "c", 4),
	} {
		if _, err := db.Exec(`INSERT INTO actors VALUES ($1, $2)`, "session-"+string(rune('a'+i)), row); err != nil {
			t.Fatal(err)
		}
	}
	got, err := Census(context.Background(), db)
	if err != nil || got[coder] != 2 || len(got) != 1 {
		t.Fatalf("census = %v, %v; want coder 2", got, err)
	}
	// One unreadable row fails the census.
	if _, err := db.Exec(`INSERT INTO actors VALUES ('session-bad', '\xffff')`); err != nil {
		t.Fatal(err)
	}
	if got, err := Census(context.Background(), db); !errors.Is(err, ErrCensus) || got != nil {
		t.Fatalf("census with a bad row = %v, %v; want ErrCensus", got, err)
	}
	// A failed read of the table fails it too.
	if _, err := db.Exec(`DROP TABLE actors CASCADE`); err != nil {
		t.Fatal(err)
	}
	if _, err := Census(context.Background(), db); err == nil {
		t.Fatal("census of a missing table succeeded")
	}
}

func TestParsePools(t *testing.T) {
	got, err := ParsePools("a/b=2, c/d ,")
	if err != nil || got[Pool{"a", "b"}] != 2 || got[Pool{"c", "d"}] != 0 || len(got) != 2 {
		t.Fatalf("ParsePools = %v, %v", got, err)
	}
	for _, bad := range []string{"nopool", "a/=1", "/b", "a/b=x", "a/b=-1"} {
		if _, err := ParsePools(bad); err == nil {
			t.Fatalf("ParsePools(%q) accepted", bad)
		}
	}
	if got, err := ParsePools(""); err != nil || len(got) != 0 {
		t.Fatalf("empty = %v, %v", got, err)
	}
}

func TestDemandPublishesIdlePoolsAtZeroAndGoesQuietWhenStale(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	now := time.Unix(1000, 0)
	d := NewDemand(map[Pool]int{coder: 2, review: 1})
	d.now = func() time.Time { return now }
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(d)

	if n := testutil.CollectAndCount(d); n != 0 {
		t.Fatalf("series before the first census = %d, want none", n)
	}
	d.Observe(Snapshot{Busy: map[Pool]int{coder: 3}})
	// Two demand series (the idle reviewer pool at 0) and two spare series.
	if n := testutil.CollectAndCount(d); n != 4 {
		t.Fatalf("series = %d, want 4", n)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	// Prometheus orders a series' labels by name: pool, pool_namespace, source.
	values := map[string]float64{}
	for _, f := range families {
		for _, m := range f.Metric {
			key := f.GetName()
			for _, l := range m.Label {
				key += "|" + l.GetValue()
			}
			values[key] = m.Gauge.GetValue()
		}
	}
	for key, want := range map[string]float64{
		"factory_worker_pool_demand|homelab-codex|factory-r-homelab|actors":           3,
		"factory_worker_pool_demand|homelab-reviewer|factory-r-homelab-review|actors": 0,
		"factory_worker_pool_spare|homelab-codex|factory-r-homelab":                   2,
		"factory_worker_pool_spare|homelab-reviewer|factory-r-homelab-review":         1,
	} {
		if got, ok := values[key]; !ok || got != want {
			t.Fatalf("%s = %v (present %v), want %v; all: %v", key, got, ok, want, values)
		}
	}

	now = now.Add(maxCensusAge + time.Second)
	if n := testutil.CollectAndCount(d); n != 0 {
		t.Fatalf("series from a stale census = %d, want none so an autoscaler holds its replicas", n)
	}
}

func TestDemandInvalidateDropsTheSeriesAtOnce(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	d := NewDemand(map[Pool]int{coder: 2})
	d.Observe(Snapshot{Busy: map[Pool]int{coder: 1}})
	if _, ok := d.Latest(); !ok || testutil.CollectAndCount(d) != 2 {
		t.Fatal("no census after Observe")
	}
	d.Invalidate()
	if _, ok := d.Latest(); ok || testutil.CollectAndCount(d) != 0 {
		t.Fatal("series survive Invalidate")
	}
}

// A pool no connector binding sends to has no pending series from the
// connector, and the autoscaler's metric needs one. The reaper publishes
// pending=0 for exactly those pools, and drops it with the actors series.
func TestDemandPublishesPendingZeroOnlyForUnboundPools(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	now := time.Unix(1000, 0)
	d := NewDemand(map[Pool]int{coder: 2, review: 2}).Unbound(map[Pool]int{review: 0})
	d.now = func() time.Time { return now }
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(d)

	d.Observe(Snapshot{Busy: map[Pool]int{review: 2}})
	families, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	pending := map[string]float64{}
	for _, f := range families {
		if f.GetName() != "factory_worker_pool_demand" {
			continue
		}
		for _, m := range f.Metric {
			var pool, source string
			for _, l := range m.Label {
				switch l.GetName() {
				case "pool":
					pool = l.GetValue()
				case "source":
					source = l.GetValue()
				}
			}
			if source == DemandSourcePending {
				pending[pool] = m.Gauge.GetValue()
			}
		}
	}
	if len(pending) != 1 || pending["homelab-reviewer"] != 0 {
		t.Fatalf("pending series = %v, want only homelab-reviewer at 0 (the connector owns the coder pool's)", pending)
	}
	// Actors for both pools, pending for the reviewer, spare for both.
	if n := testutil.CollectAndCount(d); n != 5 {
		t.Fatalf("series = %d, want 5", n)
	}

	d.Invalidate()
	if n := testutil.CollectAndCount(d); n != 0 {
		t.Fatalf("series after Invalidate = %d, want none: a pending 0 must not outlive the census", n)
	}
	d.Observe(Snapshot{Busy: map[Pool]int{}})
	now = now.Add(maxCensusAge + time.Second)
	if n := testutil.CollectAndCount(d); n != 0 {
		t.Fatalf("series from a stale census = %d, want none", n)
	}
}

// Without Unbound the reaper publishes no pending series, as before.
func TestDemandPublishesNoPendingByDefault(t *testing.T) {
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	d := NewDemand(map[Pool]int{review: 2})
	d.Observe(Snapshot{Busy: map[Pool]int{}})
	if n := testutil.CollectAndCount(d); n != 2 {
		t.Fatalf("series = %d, want actors and spare only", n)
	}
}
