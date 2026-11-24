package dispatch

import (
	"sort"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Admission order (HOME-822). A first admission takes a free slot in this
// order, first match wins:
//
//  1. Due: the issue's dueDate is on or before today + DueWindow (UTC dates).
//     Overdue first, then the nearest date, then priority, then wait age.
//  2. Starved: the run has waited StarveAfter or longer. Oldest first.
//  3. Priority urgent > high > medium > low > none, then oldest first.
//
// Wait age is the time since the connector claimed the run. Equal keys keep
// the older last-poll order, then the run ID. Only runs with no delivery
// attempt are ordered, so resumes of running sessions never move. The
// reviewer queue has its own order (HOME-685, HOME-406).
//
// Priority and dueDate come from the issue read of the run's last admission
// poll. Before ordering, a pass reads each run it has not seen, together under
// half the tick's wait, so a new claim ranks right in the pass that claimed it.
// A run whose read fails or times out ranks as none for that pass, and a changed
// priority applies on the next pass. A slow read never delays older holds.
//
// Only claimed runs are ordered. The claim phase takes runs from itsaplan in
// its order, and only while an agent has a free slot; held runs cost none.
const (
	defaultDueWindow   = 72 * time.Hour
	defaultStarveAfter = 7 * 24 * time.Hour
)

// admissionRank is what the last admission read learned about an issue.
type admissionRank struct {
	priority int       // 4 urgent .. 1 low, 0 none or unknown
	due      time.Time // UTC midnight of the due date; zero when none
}

// rankIssue reads priority and dueDate. Unknown values rank as none and no date.
func rankIssue(is *itsaplan.Issue) admissionRank {
	var r admissionRank
	if is == nil {
		return r
	}
	if is.Priority != nil {
		r.priority = map[string]int{"urgent": 4, "high": 3, "medium": 2, "low": 1}[strings.ToLower(strings.TrimSpace(*is.Priority))]
	}
	if is.DueDate != nil {
		r.due = parseDueDate(*is.DueDate)
	}
	return r
}

// parseDueDate accepts a calendar date or a timestamp and returns its UTC date.
func parseDueDate(s string) time.Time {
	s = strings.TrimSpace(s)
	if t, err := time.Parse(time.DateOnly, s); err == nil {
		return t
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		y, m, d := t.UTC().Date()
		return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
	}
	return time.Time{}
}

type admissionCandidate struct {
	id      int64
	created time.Time
	polled  time.Time
	rank    admissionRank
}

// admissionTier is 0 for due, 1 for starved and 2 otherwise.
func admissionTier(c admissionCandidate, now time.Time, window, starve time.Duration) int {
	y, m, d := now.UTC().Date()
	cutoff := time.Date(y, m, d, 0, 0, 0, 0, time.UTC).Add(window)
	if !c.rank.due.IsZero() && !c.rank.due.After(cutoff) {
		return 0
	}
	if !c.created.IsZero() && now.Sub(c.created) >= starve {
		return 1
	}
	return 2
}

// sortAdmissions orders candidates by the rule above.
func sortAdmissions(cs []admissionCandidate, now time.Time, window, starve time.Duration) {
	tier := make(map[int64]int, len(cs))
	for _, c := range cs {
		tier[c.id] = admissionTier(c, now, window, starve)
	}
	sort.SliceStable(cs, func(i, j int) bool {
		a, b := cs[i], cs[j]
		if ta, tb := tier[a.id], tier[b.id]; ta != tb {
			return ta < tb
		}
		if tier[a.id] == 0 && !a.rank.due.Equal(b.rank.due) {
			return a.rank.due.Before(b.rank.due)
		}
		if tier[a.id] != 1 && a.rank.priority != b.rank.priority {
			return a.rank.priority > b.rank.priority
		}
		if !a.created.Equal(b.created) {
			return a.created.Before(b.created)
		}
		if !a.polled.Equal(b.polled) {
			return a.polled.Before(b.polled)
		}
		return a.id < b.id
	})
}

// orderAdmissions orders runs for one pass with the ranks the last admission
// reads recorded. A run with no rank yet ranks as none.
func (e *Engine) orderAdmissions(runs []*store.Run) {
	cs := make([]admissionCandidate, len(runs))
	byID := make(map[int64]*store.Run, len(runs))
	for i, r := range runs {
		c := admissionCandidate{id: r.ID, created: r.CreatedAt}
		if at, ok := e.admissionPolled.Load(r.ID); ok {
			c.polled = at.(time.Time)
		}
		if rk, ok := e.admissionRanks.Load(r.ID); ok {
			c.rank = rk.(admissionRank)
		}
		cs[i], byID[r.ID] = c, r
	}
	sortAdmissions(cs, e.cfg.Now(), e.cfg.DueWindow, e.cfg.StarveAfter)
	for i, c := range cs {
		runs[i] = byID[c.id]
	}
}
