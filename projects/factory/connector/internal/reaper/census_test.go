package reaper

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"google.golang.org/protobuf/encoding/protowire"
)

// actorIn encodes an Actor with the given state on the named worker.
func actorIn(namespace, pool, pod string, state uint64) []byte {
	field := func(n protowire.Number, value []byte) []byte {
		return protowire.AppendBytes(protowire.AppendTag(nil, n, protowire.BytesType), value)
	}
	number := func(n protowire.Number, value uint64) []byte {
		return protowire.AppendVarint(protowire.AppendTag(nil, n, protowire.VarintType), value)
	}
	var worker []byte
	if pod != "" {
		worker = append(field(1, []byte(namespace)), field(2, []byte(pool))...)
		worker = append(worker, field(3, []byte(pod))...)
		worker = append(worker, field(4, []byte("uid-"+pod))...)
	}
	status := number(1, state)
	if worker != nil {
		status = append(status, field(2, worker)...)
	}
	return field(5, status)
}

func TestBusyWorkersCountsPodsThatHostAnActor(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	got := BusyWorkers([][]byte{
		actorIn(coder.Namespace, coder.Name, "homelab-codex-a", stateRunning),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-a", stateRunning), // same pod counts once
		actorIn(coder.Namespace, coder.Name, "homelab-codex-b", stateResuming),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-c", stateSuspending),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-d", stateReverting),
		actorIn(coder.Namespace, coder.Name, "homelab-codex-e", 4), // SUSPENDED: no worker
		actorIn(coder.Namespace, coder.Name, "homelab-codex-f", 5), // CRASHED
		actorIn(coder.Namespace, coder.Name, "homelab-codex-g", 6), // DELETING
		actorIn("", "", "", stateRunning),                          // no assignment
		actorIn(review.Namespace, review.Name, "homelab-reviewer-a", stateRunning),
		{0xff, 0xff}, // malformed
	})
	if got[coder] != 4 || got[review] != 1 || len(got) != 2 {
		t.Fatalf("busy workers = %v, want coder 4 and reviewer 1", got)
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
	d.Observe(map[Pool]int{coder: 3})
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
