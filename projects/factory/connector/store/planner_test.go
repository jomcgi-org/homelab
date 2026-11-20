package store_test

import (
	"context"
	_ "embed"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/planner"
)

//go:embed planner_breaker_test.sql
var plannerBreakerTest string

func TestPlannerBreakerAfterUnqualifiedReservations(t *testing.T) {
	if _, err := open(t).DB.Exec(plannerBreakerTest); err != nil {
		t.Fatal(err)
	}
}

func TestPlannerScheduleSyncKeepsHistoryAndDisablesRemovedSchedules(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	w, err := st.TryAcquireWriter(ctx, "test")
	if err != nil || w == nil {
		t.Fatalf("acquire writer = %v, %v", w, err)
	}
	defer w.Release()
	schedule := planner.Schedule{
		Name: "smoke", Namespace: "homelab:docs", Ceiling: "delegate",
		MinInterval: time.Hour, Cooldown: 24 * time.Hour, PerRun: 1,
		IssueTitle: "Check documentation", Instructions: "Verify the documented claim.",
	}
	if err := w.SyncPlannerSchedules(ctx, []planner.Schedule{schedule}); err != nil {
		t.Fatal(err)
	}
	var admitted bool
	if err := st.DB.QueryRow(`SELECT admit_planner_run('smoke', 'first')`).Scan(&admitted); err != nil || !admitted {
		t.Fatalf("first admission = %v, %v", admitted, err)
	}
	if _, err := st.DB.Exec(`SELECT finish_planner_run('first');
		UPDATE planner_run SET started_at = now() - interval '2 hours' WHERE run_id = 'first'`); err != nil {
		t.Fatal(err)
	}
	if err := w.SyncPlannerSchedules(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if err := st.DB.QueryRow(`SELECT admit_planner_run('smoke', 'removed')`).Scan(&admitted); err != nil || admitted {
		t.Fatalf("removed schedule admission = %v, %v", admitted, err)
	}
	schedule.Ceiling = "backlog"
	schedule.PerRun = 2
	if err := w.SyncPlannerSchedules(ctx, []planner.Schedule{schedule}); err != nil {
		t.Fatal(err)
	}
	var recorded bool
	err = st.DB.QueryRow(`SELECT enabled AND low_yield = 1 AND ceiling = 'backlog' AND per_run = 2
		AND min_interval = interval '1 hour' AND cooldown = interval '1 day'
		AND EXISTS (SELECT FROM planner_run WHERE schedule = name AND run_id = 'first' AND finished_at IS NOT NULL)
		FROM planner_schedule WHERE name = 'smoke'`).Scan(&recorded)
	if err != nil || !recorded {
		t.Fatalf("schedule and history restored = %v, %v", recorded, err)
	}
	if err := st.DB.QueryRow(`SELECT admit_planner_run('smoke', 'second')`).Scan(&admitted); err != nil || !admitted {
		t.Fatalf("restored schedule admission = %v, %v", admitted, err)
	}
}
