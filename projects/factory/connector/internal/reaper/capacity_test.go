package reaper

import (
	"context"
	"database/sql"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
)

func worker(name, pool string, phase corev1.PodPhase, ready corev1.ConditionStatus, terminating bool) *corev1.Pod {
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "factory-r-homelab", Labels: map[string]string{workerPoolLabel: pool}},
		Status: corev1.PodStatus{
			Phase:      phase,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: ready}},
		},
	}
	if terminating {
		now := metav1.Now()
		p.DeletionTimestamp = &now
	}
	return p
}

func TestCapacitiesCountsReadyWorkersOfTheConfiguredPool(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	client := fake.NewSimpleClientset(
		worker("a", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false),
		worker("b", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false),
		worker("starting", "homelab-codex", corev1.PodRunning, corev1.ConditionFalse, false),
		worker("pending", "homelab-codex", corev1.PodPending, corev1.ConditionFalse, false),
		worker("leaving", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, true),
		worker("other", "homelab-reviewer", corev1.PodRunning, corev1.ConditionTrue, false),
	)
	got, err := Capacities(context.Background(), client.CoreV1(), []Pool{coder, {"factory-r-empty", "idle"}}, map[Pool]int{coder: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != (Capacity{Pool: coder, Ready: 2, Busy: 1}) || got[1] != (Capacity{Pool: Pool{"factory-r-empty", "idle"}}) {
		t.Fatalf("capacities = %+v, want coder ready 2 busy 1 and the empty pool 0/0", got)
	}
}

func TestRecordCapacityNeedsTheCurrentEpoch(t *testing.T) {
	db := pgtestDB(t)
	lease, err := TryLease(context.Background(), db)
	if err != nil || lease == nil {
		t.Fatalf("lease=%v err=%v", lease, err)
	}
	t.Cleanup(func() { lease.Close() })
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	client := fake.NewSimpleClientset(worker("a", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false))
	if err := RecordCapacity(context.Background(), lease, client.CoreV1(), []Pool{coder}, map[Pool]int{coder: 1}); err != nil {
		t.Fatal(err)
	}
	var ready, busy int
	if err := db.QueryRow(`SELECT ready, busy FROM worker_pool_capacity WHERE namespace = $1 AND pool = $2`,
		coder.Namespace, coder.Name).Scan(&ready, &busy); err != nil || ready != 1 || busy != 1 {
		t.Fatalf("row ready=%d busy=%d err=%v", ready, busy, err)
	}
	// A second pass updates the row in place.
	if err := RecordCapacity(context.Background(), lease, client.CoreV1(), []Pool{coder}, nil); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`SELECT busy FROM worker_pool_capacity`).Scan(&busy); err != nil || busy != 0 {
		t.Fatalf("updated busy=%d err=%v", busy, err)
	}
	// Another reaper took over: this one writes nothing.
	if _, err := db.Exec(`UPDATE reaper_writer SET epoch = epoch + 1; DELETE FROM worker_pool_capacity`); err != nil {
		t.Fatal(err)
	}
	if err := RecordCapacity(context.Background(), lease, client.CoreV1(), []Pool{coder}, nil); !errors.Is(err, ErrNotLeader) {
		t.Fatalf("stale epoch: %v", err)
	}
	var n int
	if err := db.QueryRow(`SELECT count(*) FROM worker_pool_capacity`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("a stale reaper wrote %d rows (err %v)", n, err)
	}
}

// pgtestDB opens a fresh migrated database.
func pgtestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}
