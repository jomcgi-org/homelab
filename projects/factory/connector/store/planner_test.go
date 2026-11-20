package store_test

import (
	_ "embed"
	"testing"
)

//go:embed planner_breaker_test.sql
var plannerBreakerTest string

func TestPlannerBreakerAfterUnqualifiedReservations(t *testing.T) {
	if _, err := open(t).DB.Exec(plannerBreakerTest); err != nil {
		t.Fatal(err)
	}
}
