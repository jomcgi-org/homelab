package reaper

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
)

// workerPoolLabel names the WorkerPool on each of its worker pods (Substrate's
// WorkerPool controller sets it).
const workerPoolLabel = "ate.dev/worker-pool"

// Capacity is one pool's workers: pods that can take an actor, and pods that
// host one.
type Capacity struct {
	Pool  Pool
	Ready int
	Busy  int
}

// podReady reports a worker pod that can take an actor: running, ready and
// not being deleted.
func podReady(p *corev1.Pod) bool {
	if p.DeletionTimestamp != nil || p.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

// Capacities reads each configured pool's ready pods and joins them with the
// census's busy count. Any read error fails the whole read, so a partial
// answer never reaches the table.
func Capacities(ctx context.Context, pods core.PodsGetter, pools []Pool, busy map[Pool]int) ([]Capacity, error) {
	out := make([]Capacity, 0, len(pools))
	for _, pool := range pools {
		list, err := pods.Pods(pool.Namespace).List(ctx, metav1.ListOptions{LabelSelector: workerPoolLabel + "=" + pool.Name})
		if err != nil {
			return nil, fmt.Errorf("list workers of %s/%s: %w", pool.Namespace, pool.Name, err)
		}
		c := Capacity{Pool: pool, Busy: busy[pool]}
		for i := range list.Items {
			if podReady(&list.Items[i]) {
				c.Ready++
			}
		}
		out = append(out, c)
	}
	return out, nil
}

// record upserts the capacities while the lease's epoch is current.
func (l *Lease) record(ctx context.Context, caps []Capacity) error {
	if l == nil || l.conn == nil {
		return ErrNotLeader
	}
	tx, err := l.conn.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrNotLeader, err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit
	var epoch int64
	if err := tx.QueryRowContext(ctx, `SELECT epoch FROM reaper_writer WHERE id = 1 FOR UPDATE`).Scan(&epoch); err != nil {
		return fmt.Errorf("%w: %v", ErrNotLeader, err)
	}
	if epoch != l.Epoch {
		return ErrNotLeader
	}
	for _, c := range caps {
		if _, err := tx.ExecContext(ctx, `INSERT INTO worker_pool_capacity (namespace, pool, ready, busy)
			VALUES ($1, $2, $3, $4)
			ON CONFLICT (namespace, pool) DO UPDATE SET ready = excluded.ready, busy = excluded.busy, observed_at = now()`,
			c.Pool.Namespace, c.Pool.Name, c.Ready, c.Busy); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// RecordCapacity publishes the configured pools' capacity for the connector.
func RecordCapacity(ctx context.Context, lease *Lease, pods core.PodsGetter, pools []Pool, busy map[Pool]int) error {
	if len(pools) == 0 {
		return nil
	}
	caps, err := Capacities(ctx, pods, pools, busy)
	if err != nil {
		return err
	}
	return lease.record(ctx, caps)
}
