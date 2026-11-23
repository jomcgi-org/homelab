package metrics

import (
	"context"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// Pool names one Substrate WorkerPool. A binding's Agent and its WorkerPool
// share a namespace and a name (chart templates/_binding.tpl).
type Pool struct{ Namespace, Name string }

// PoolDemandSourcePending is the source label of the workers the connector's
// own unfinished sends need. The worker reaper publishes the other sources of
// the same series (ARCHITECTURE.md "Worker pool autoscaling").
const PoolDemandSourcePending = "pending"

const poolDemandTimeout = 5 * time.Second

// PoolDemand publishes factory_worker_pool_demand{source="pending"}: for each
// pool, the delivery attempts that still need an actor and have no accepted
// task. It reads the database on each scrape, so a send recorded a moment ago
// shows at once; no tick has to run first. A pool the connector dispatches to
// always has a series, with 0 when idle, so the metric never goes missing for
// a quiet pool (a missing series stops an autoscaler from scaling down).
//
// A failed read publishes nothing for that scrape, which an autoscaler reads
// as "no data" and answers by holding the replica count. Labels come from
// configuration, never from run data.
type PoolDemand struct {
	pools   []Pool
	pending func(context.Context) (map[Pool]int64, error)
	onError func()
	desc    *prometheus.Desc
}

// NewPoolDemand returns the collector. pools are the pools the connector
// dispatches to; pending counts attempts by pool; onError counts a failed read.
func NewPoolDemand(pools []Pool, pending func(context.Context) (map[Pool]int64, error), onError func()) *PoolDemand {
	return &PoolDemand{
		pools: pools, pending: pending, onError: onError,
		desc: prometheus.NewDesc("factory_worker_pool_demand",
			"Workers a WorkerPool needs, by source. Sum the sources for the pool's demand (pending: attempts recorded and not yet accepted by kagent).",
			[]string{"pool_namespace", "pool", "source"}, nil),
	}
}

// Describe implements prometheus.Collector.
func (d *PoolDemand) Describe(ch chan<- *prometheus.Desc) { ch <- d.desc }

// Collect implements prometheus.Collector.
func (d *PoolDemand) Collect(ch chan<- prometheus.Metric) {
	ctx, cancel := context.WithTimeout(context.Background(), poolDemandTimeout)
	defer cancel()
	counts, err := d.pending(ctx)
	if err != nil {
		if d.onError != nil {
			d.onError()
		}
		return
	}
	seen := make(map[Pool]bool, len(d.pools))
	emit := func(p Pool) {
		if seen[p] {
			return
		}
		seen[p] = true
		ch <- prometheus.MustNewConstMetric(d.desc, prometheus.GaugeValue, float64(counts[p]),
			p.Namespace, p.Name, PoolDemandSourcePending)
	}
	for _, p := range d.pools {
		emit(p)
	}
	for p := range counts {
		emit(p)
	}
}
