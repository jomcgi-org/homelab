package reaper

import (
	"context"
	"database/sql"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// Pool names a Substrate WorkerPool.
type Pool struct{ Namespace, Name string }

// Actor states (ateapi.v1.ActorState) in which an actor holds its worker.
// SUSPENDED and CRASHED hold none, and DELETING is about to release it.
const (
	stateResuming   = 1
	stateRunning    = 2
	stateSuspending = 3
	stateReverting  = 7
)

func holdsWorker(state uint64) bool {
	switch state {
	case stateResuming, stateRunning, stateSuspending, stateReverting:
		return true
	}
	return false
}

// workerPool returns the pool named in an actor's worker assignment
// (WorkerAssignment.worker_pool, field 2).
func workerPool(proto []byte) Pool {
	w := bytesField(bytesField(proto, 5), 2)
	return Pool{string(bytesField(w, 1)), string(bytesField(w, 2))}
}

// BusyWorkers counts, for each pool, the worker pods that host an actor. A
// pool's worker runs one actor at a time (ARCHITECTURE.md "Repository
// bindings"), so distinct pods are the workers a pool cannot lose. Absent,
// unknown or malformed fields count nothing: this only keeps a worker, it
// never authorizes an action against one.
func BusyWorkers(protos [][]byte) map[Pool]int {
	pods := make(map[Pool]map[string]struct{})
	for _, proto := range protos {
		p, state, _ := actorPlacement(proto)
		if p.pod == "" || !holdsWorker(state) {
			continue
		}
		pool := workerPool(proto)
		if pool.Name == "" {
			continue
		}
		if pods[pool] == nil {
			pods[pool] = make(map[string]struct{})
		}
		pods[pool][p.namespace+"/"+p.pod] = struct{}{}
	}
	out := make(map[Pool]int, len(pods))
	for pool, set := range pods {
		out[pool] = len(set)
	}
	return out
}

// Census reads every session actor and counts the busy workers per pool.
func Census(ctx context.Context, actors *sql.DB) (map[Pool]int, error) {
	rows, err := actors.QueryContext(ctx, `SELECT proto FROM public.reaper_actors()`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var protos [][]byte
	for rows.Next() {
		var proto []byte
		if err := rows.Scan(&proto); err != nil {
			return nil, err
		}
		protos = append(protos, proto)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return BusyWorkers(protos), nil
}

// DemandSourceActors is the source label of the workers that host actors. The
// connector publishes the "pending" source of the same series.
const DemandSourceActors = "actors"

// maxCensusAge is how long a census stays valid. An older one publishes
// nothing, which an autoscaler reads as "no data" and answers by holding its
// replica count. Three scan intervals.
const maxCensusAge = 45 * time.Second

// Demand publishes the census as factory_worker_pool_demand{source="actors"}
// and each configured pool's spare workers as factory_worker_pool_spare.
// Every configured pool has a series, 0 when idle, so the metric never goes
// missing for a quiet pool (a missing series stops an autoscaler from scaling
// down). Labels come from configuration and Substrate, never from run data.
type Demand struct {
	spare map[Pool]int
	now   func() time.Time

	mu    sync.Mutex
	busy  map[Pool]int
	taken time.Time

	demand *prometheus.Desc
	spares *prometheus.Desc
}

// NewDemand returns the publisher. spare names the pools to publish and each
// pool's spare workers (the headroom an autoscaler adds while a pool has any
// demand).
func NewDemand(spare map[Pool]int) *Demand {
	return &Demand{
		spare: spare, now: time.Now,
		demand: prometheus.NewDesc("factory_worker_pool_demand",
			"Workers a WorkerPool needs, by source. Sum the sources for the pool's demand (actors: worker pods that host an actor).",
			[]string{"pool_namespace", "pool", "source"}, nil),
		spares: prometheus.NewDesc("factory_worker_pool_spare",
			"Spare workers an autoscaler adds to a pool's demand while the pool has any.",
			[]string{"pool_namespace", "pool"}, nil),
	}
}

// ParsePools reads "namespace/name=spare,..." into a pool-to-spare map. An
// entry with no "=spare" has spare 0. Empty input is an empty map.
func ParsePools(s string) (map[Pool]int, error) {
	out := map[Pool]int{}
	for _, entry := range strings.Split(s, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		name, spare, hasSpare := strings.Cut(entry, "=")
		ns, pool, ok := strings.Cut(name, "/")
		if !ok || ns == "" || pool == "" {
			return nil, &parseError{entry}
		}
		n := 0
		if hasSpare {
			var err error
			if n, err = strconv.Atoi(spare); err != nil || n < 0 {
				return nil, &parseError{entry}
			}
		}
		out[Pool{ns, pool}] = n
	}
	return out, nil
}

type parseError struct{ entry string }

func (e *parseError) Error() string {
	return "reaper: pool entry " + strconv.Quote(e.entry) + ` is not "namespace/name" or "namespace/name=spare"`
}

// Observe records a successful census.
func (d *Demand) Observe(busy map[Pool]int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.busy, d.taken = busy, d.now()
}

// Describe implements prometheus.Collector.
func (d *Demand) Describe(ch chan<- *prometheus.Desc) { ch <- d.demand; ch <- d.spares }

// Collect implements prometheus.Collector.
func (d *Demand) Collect(ch chan<- prometheus.Metric) {
	d.mu.Lock()
	busy, taken := d.busy, d.taken
	d.mu.Unlock()
	if busy == nil || d.now().Sub(taken) > maxCensusAge {
		return
	}
	pools := make(map[Pool]bool, len(d.spare)+len(busy))
	for p := range d.spare {
		pools[p] = true
	}
	for p := range busy {
		pools[p] = true
	}
	for p := range pools {
		ch <- prometheus.MustNewConstMetric(d.demand, prometheus.GaugeValue, float64(busy[p]),
			p.Namespace, p.Name, DemandSourceActors)
	}
	for p, n := range d.spare {
		ch <- prometheus.MustNewConstMetric(d.spares, prometheus.GaugeValue, float64(n), p.Namespace, p.Name)
	}
}
