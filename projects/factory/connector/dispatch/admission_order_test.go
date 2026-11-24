package dispatch

import (
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

var orderNow = time.Date(2026, 11, 24, 15, 30, 0, 0, time.UTC)

func ptr(s string) *string { return &s }

func orderCand(id int64, priority, due string, waited time.Duration) admissionCandidate {
	is := &itsaplan.Issue{}
	if priority != "" {
		is.Priority = ptr(priority)
	}
	if due != "" {
		is.DueDate = ptr(due)
	}
	return admissionCandidate{id: id, created: orderNow.Add(-waited), rank: rankIssue(is)}
}

func orderOf(cs ...admissionCandidate) []int64 {
	sortAdmissions(cs, orderNow, defaultDueWindow, defaultStarveAfter)
	out := make([]int64, len(cs))
	for i, c := range cs {
		out[i] = c.id
	}
	return out
}

func sameOrder(t *testing.T, got []int64, want ...int64) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

const hour = time.Hour

func TestAdmissionOrderByPriority(t *testing.T) {
	got := orderOf(
		orderCand(1, "low", "", 5*hour),
		orderCand(2, "", "", 4*hour),
		orderCand(3, "medium", "", 3*hour),
		orderCand(4, "urgent", "", 2*hour),
		orderCand(5, "high", "", 1*hour),
	)
	sameOrder(t, got, 4, 5, 3, 1, 2)
}

func TestAdmissionOrderNoneAndUnknownPriorityRankLast(t *testing.T) {
	got := orderOf(
		orderCand(1, "", "", 30*hour),
		orderCand(2, "someday", "", 20*hour),
		orderCand(3, "LOW", "", 1*hour), // case does not matter
	)
	sameOrder(t, got, 3, 1, 2)
}

func TestAdmissionOrderTiesBreakByWaitAge(t *testing.T) {
	got := orderOf(
		orderCand(1, "high", "", 2*hour),
		orderCand(2, "high", "", 9*hour),
		orderCand(3, "high", "", 5*hour),
	)
	sameOrder(t, got, 2, 3, 1)
}

func TestAdmissionOrderDueDateJumpsThePriorityQueue(t *testing.T) {
	got := orderOf(
		orderCand(1, "urgent", "", 50*hour),
		orderCand(2, "low", "2026-11-27", 1*hour),  // today + 3: inside the window
		orderCand(3, "none", "2026-11-22", 1*hour), // overdue
		orderCand(4, "high", "2026-11-28", 1*hour), // today + 4: outside the window
		orderCand(5, "medium", "2026-11-24", 1*hour),
	)
	// Overdue, then nearest date, then the window edge; the rest by priority.
	sameOrder(t, got, 3, 5, 2, 1, 4)
}

func TestAdmissionOrderDueDatesTieByPriorityThenAge(t *testing.T) {
	got := orderOf(
		orderCand(1, "low", "2026-11-25", 9*hour),
		orderCand(2, "urgent", "2026-11-25", 1*hour),
		orderCand(3, "urgent", "2026-11-25", 5*hour),
	)
	sameOrder(t, got, 3, 2, 1)
}

func TestAdmissionOrderDueDateOutsideWindowDoesNotJump(t *testing.T) {
	got := orderOf(
		orderCand(1, "low", "2026-12-24", 1*hour),
		orderCand(2, "medium", "", 1*hour),
	)
	sameOrder(t, got, 2, 1)
}

func TestAdmissionOrderStarvedRunsPrecedePriority(t *testing.T) {
	got := orderOf(
		orderCand(1, "urgent", "", 1*hour),
		orderCand(2, "low", "", 7*24*hour), // exactly the bound
		orderCand(3, "none", "", 9*24*hour),
		orderCand(4, "high", "", 6*24*hour), // not starved yet
	)
	// Starved runs go oldest first; priority does not reorder them.
	sameOrder(t, got, 3, 2, 1, 4)
}

func TestAdmissionOrderDueRunsPrecedeStarvedRuns(t *testing.T) {
	got := orderOf(
		orderCand(1, "none", "", 30*24*hour),
		orderCand(2, "low", "2026-11-26", 1*hour),
	)
	sameOrder(t, got, 2, 1)
}

func TestAdmissionOrderUnparseableDueDateIsIgnored(t *testing.T) {
	got := orderOf(
		orderCand(1, "low", "soon", 1*hour),
		orderCand(2, "medium", "", 1*hour),
	)
	sameOrder(t, got, 2, 1)
	if !parseDueDate("2026-11-24T23:59:59-05:00").Equal(time.Date(2026, 11, 25, 0, 0, 0, 0, time.UTC)) {
		t.Fatal("a timestamp due date must map to its UTC date")
	}
}

func TestAdmissionOrderIsDeterministic(t *testing.T) {
	a := orderCand(1, "high", "", hour)
	b := orderCand(2, "high", "", hour)
	sameOrder(t, orderOf(b, a), 1, 2)
	a.polled, b.polled = orderNow, orderNow.Add(-time.Minute)
	sameOrder(t, orderOf(a, b), 2, 1) // the older poll goes first
}
