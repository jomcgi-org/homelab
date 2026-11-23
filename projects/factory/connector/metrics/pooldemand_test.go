package metrics

import (
	"context"
	"errors"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestPoolDemandSeedsIdlePoolsAtZero(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	review := Pool{"factory-r-homelab-review", "homelab-reviewer"}
	d := NewPoolDemand([]Pool{coder, review}, func(context.Context) (map[Pool]int64, error) {
		return map[Pool]int64{coder: 3}, nil
	}, nil)
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(d)
	got := func(p Pool) float64 {
		t.Helper()
		families, err := reg.Gather()
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range families {
			for _, m := range f.Metric {
				l := map[string]string{}
				for _, lp := range m.Label {
					l[lp.GetName()] = lp.GetValue()
				}
				if l["pool_namespace"] == p.Namespace && l["pool"] == p.Name && l["source"] == "pending" {
					return m.Gauge.GetValue()
				}
			}
		}
		t.Fatalf("no series for %v", p)
		return -1
	}
	if v := got(coder); v != 3 {
		t.Fatalf("coder pending = %v, want 3", v)
	}
	if v := got(review); v != 0 {
		t.Fatalf("idle reviewer pool pending = %v, want 0 (a missing series would stop scale-down)", v)
	}
	if n := testutil.CollectAndCount(d); n != 2 {
		t.Fatalf("series = %d, want 2", n)
	}
}

func TestPoolDemandPublishesAnUnconfiguredPoolItCounts(t *testing.T) {
	other := Pool{"factory-r-other", "other-codex"}
	d := NewPoolDemand(nil, func(context.Context) (map[Pool]int64, error) {
		return map[Pool]int64{other: 1}, nil
	}, nil)
	if n := testutil.CollectAndCount(d); n != 1 {
		t.Fatalf("series = %d, want 1", n)
	}
}

func TestPoolDemandPublishesNothingWhenTheReadFails(t *testing.T) {
	failures := 0
	d := NewPoolDemand([]Pool{{"ns", "pool"}}, func(context.Context) (map[Pool]int64, error) {
		return nil, errors.New("database down")
	}, func() { failures++ })
	if n := testutil.CollectAndCount(d); n != 0 {
		t.Fatalf("series = %d, want none so an autoscaler holds its replicas", n)
	}
	if failures != 1 {
		t.Fatalf("failures counted = %d, want 1", failures)
	}
}
