package reaper

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/protobuf/encoding/protowire"
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

// ErrCensus means an actor row could not be read with certainty. The census
// then publishes nothing: a row that decodes to "no worker" would otherwise
// lower the demand under a live actor.
var ErrCensus = errors.New("reaper: actor row unreadable")

// strictField returns the bytes of the first field number of the wire type in
// msg, whether it is present, and an error when msg is not well-formed protobuf.
func strictField(msg []byte, number protowire.Number, typ protowire.Type) ([]byte, bool, error) {
	var found []byte
	var ok bool
	for len(msg) > 0 {
		n, t, k := protowire.ConsumeTag(msg)
		if k < 0 {
			return nil, false, protowire.ParseError(k)
		}
		msg = msg[k:]
		size := protowire.ConsumeFieldValue(n, t, msg)
		if size < 0 {
			return nil, false, protowire.ParseError(size)
		}
		if n == number && t == typ && !ok {
			ok = true
			switch t {
			case protowire.BytesType:
				found, _ = protowire.ConsumeBytes(msg)
			default:
				found = msg[:size]
			}
		}
		msg = msg[size:]
	}
	return found, ok, nil
}

// decodeHolder reads the state of one actor row and, when the actor holds a
// worker, the pool and pod. Every field that decides the count must be present
// and well-formed: the actor's state (1 to 7), and for a holding state the
// worker's namespace, pool and pod.
func decodeHolder(proto []byte) (holds bool, pool Pool, pod string, err error) {
	bad := func(why string) (bool, Pool, string, error) {
		return false, Pool{}, "", fmt.Errorf("%w: %s", ErrCensus, why)
	}
	status, ok, err := strictField(proto, 5, protowire.BytesType)
	if err != nil || !ok {
		return bad("no readable status")
	}
	raw, ok, err := strictField(status, 1, protowire.VarintType)
	if err != nil || !ok {
		return bad("no readable state")
	}
	state, n := protowire.ConsumeVarint(raw)
	if n < 0 || state < 1 || state > 7 {
		return bad("state outside 1 to 7")
	}
	if !holdsWorker(state) {
		return false, Pool{}, "", nil
	}
	worker, ok, err := strictField(status, 2, protowire.BytesType)
	if err != nil || !ok {
		return bad("holding state without a readable worker assignment")
	}
	var parts [3][]byte
	for i, field := range []protowire.Number{1, 2, 3} {
		v, ok, err := strictField(worker, field, protowire.BytesType)
		if err != nil || !ok || len(v) == 0 {
			return bad("holding state with an empty worker namespace, pool or pod")
		}
		parts[i] = v
	}
	return true, Pool{string(parts[0]), string(parts[1])}, string(parts[2]), nil
}

// BusyPods lists, for each pool, the worker pods that host an actor. A pool's
// worker runs one actor at a time (ARCHITECTURE.md "Repository bindings"), so
// these are the workers a pool cannot lose. Unlike the deletion path, which
// ignores what it cannot read, it fails on any row it cannot read (ErrCensus):
// a low count under a live actor would let an autoscaler remove its worker.
func BusyPods(protos [][]byte) (map[Pool]map[string]struct{}, error) {
	pods := make(map[Pool]map[string]struct{})
	for _, proto := range protos {
		holds, pool, pod, err := decodeHolder(proto)
		if err != nil {
			return nil, err
		}
		if !holds {
			continue
		}
		if pods[pool] == nil {
			pods[pool] = make(map[string]struct{})
		}
		pods[pool][pod] = struct{}{}
	}
	return pods, nil
}

// BusyWorkers counts, for each pool, the worker pods that host an actor.
func BusyWorkers(protos [][]byte) (map[Pool]int, error) {
	pods, err := BusyPods(protos)
	if err != nil {
		return nil, err
	}
	out := make(map[Pool]int, len(pods))
	for pool, set := range pods {
		out[pool] = len(set)
	}
	return out, nil
}

// Snapshot is one census: the busy workers of each pool, by count and by name.
type Snapshot struct {
	Busy map[Pool]int
	Pods map[Pool]map[string]struct{}
}

// Take reads every session actor once.
func Take(ctx context.Context, actors *sql.DB) (Snapshot, error) {
	rows, err := actors.QueryContext(ctx, `SELECT proto FROM public.reaper_actors()`)
	if err != nil {
		return Snapshot{}, err
	}
	defer rows.Close()
	var protos [][]byte
	for rows.Next() {
		var proto []byte
		if err := rows.Scan(&proto); err != nil {
			return Snapshot{}, err
		}
		protos = append(protos, proto)
	}
	if err := rows.Err(); err != nil {
		return Snapshot{}, err
	}
	pods, err := BusyPods(protos)
	if err != nil {
		return Snapshot{}, err
	}
	busy := make(map[Pool]int, len(pods))
	for pool, set := range pods {
		busy[pool] = len(set)
	}
	return Snapshot{Busy: busy, Pods: pods}, nil
}

// Census counts the busy workers per pool.
func Census(ctx context.Context, actors *sql.DB) (map[Pool]int, error) {
	snap, err := Take(ctx, actors)
	if err != nil {
		return nil, err
	}
	return snap.Busy, nil
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
	pods  map[Pool]map[string]struct{}
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
func (d *Demand) Observe(snap Snapshot) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.busy, d.pods, d.taken = snap.Busy, snap.Pods, d.now()
}

// Invalidate drops the last census, so the series disappear at once. A census
// that fails or cannot be read calls it: the metric then has no value and an
// autoscaler holds its replicas, instead of waiting out maxCensusAge on a
// number that may be too low.
func (d *Demand) Invalidate() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.busy, d.pods = nil, nil
}

// Latest returns the last census, or false when there is none or it is too old.
func (d *Demand) Latest() (Snapshot, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.busy == nil || d.now().Sub(d.taken) > maxCensusAge {
		return Snapshot{}, false
	}
	return Snapshot{Busy: d.busy, Pods: d.pods}, true
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
