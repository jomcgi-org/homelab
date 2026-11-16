package dispatch_test

import (
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// quiet moves every finished attempt past the sweep's quiet period, which
// is measured on the database's clock.
func (v *env) quiet() {
	v.t.Helper()
	if _, err := v.st.DB.Exec(`UPDATE delivery_attempt SET finished_at = finished_at - interval '1 hour' WHERE finished_at IS NOT NULL`); err != nil {
		v.t.Fatal(err)
	}
}

// sweep advances past the sweep spacing and ticks.
func (v *env) sweep() {
	v.t.Helper()
	v.clk.Advance(11 * time.Minute)
	v.tick(v.e)
}

func (v *env) deletedFor(reason string) float64 {
	return testutil.ToFloat64(v.m.SessionsDeleted.WithLabelValues(reason))
}

func TestSweepDeletesAMergedWorkItemsSessionAndAResumeOpensANewOne(t *testing.T) {
	v := newDeliveryEnv(t)
	session := v.delivered()
	v.gh.green("sha-1")
	v.poll()
	v.gh.merge(1, "merge-sha-1")
	v.poll()
	if w := v.work(101); w.State != store.WorkDone {
		t.Fatalf("work item = %s, want done", w.State)
	}
	v.sweep()
	if d := v.kg.Deleted(); len(d) != 0 {
		t.Fatalf("deleted %v within the quiet period", d)
	}
	v.quiet()
	v.sweep()
	if d := v.kg.Deleted(); !slices.Equal(d, []string{session}) {
		t.Fatalf("deleted %v, want the merged work item's session %s", d, session)
	}
	if w := v.work(101); w.SessionID != "" || w.State != store.WorkDone {
		t.Fatalf("work item = %+v, want done with no session", w)
	}
	if got := v.deletedFor(store.SessionDone); got != 1 {
		t.Fatalf("done deletions = %v, want 1", got)
	}
	calls := v.kg.Calls()
	v.tick(v.e)
	if v.kg.Calls() != calls {
		t.Fatal("a sweep ran again within the sweep spacing")
	}

	// A later run on the issue opens a new session with the issue brief.
	v.its.add(2, "coder", 101, "HL-7", "Also tidy the helper.")
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	sends := v.kg.Sends()
	last := sends[len(sends)-1].msg
	if last.ContextID != "" || last.MessageID != "itsaplan-run/2/replacement-1-0" ||
		!strings.Contains(last.Parts[0].Text, "### Issue HL-7: Issue 101 title") {
		t.Fatalf("message %q in %q, want a new session with the brief:\n%s", last.MessageID, last.ContextID, last.Parts[0].Text)
	}
	if w := v.work(101); w.SessionID == "" || w.SessionID == session {
		t.Fatalf("work item session = %q, want a new one", w.SessionID)
	}
}

func TestSweepDeletesOnlyTheCallersReplacedSessionsAndRetries(t *testing.T) {
	v := newEnv(t)
	v.its.add(1, "coder", 101, "HL-7", "Fix the flaky test.")
	v.tick(v.e)
	old := v.work(101).SessionID
	v.kg.finish(v.kg.lastTask(), kagent.StateCompleted, "Pushed factory/HL-7.", nil)
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	// The work item moved on to another session.
	if _, err := v.st.DB.Exec(`UPDATE work_item SET session_id = 'ctx-new' WHERE issue_id = 101`); err != nil {
		t.Fatal(err)
	}
	v.quiet()

	v.kg.mu.Lock()
	v.kg.sessions[old].Creator = "someone-else"
	v.kg.mu.Unlock()
	v.sweep()
	if d := v.kg.Deleted(); len(d) != 0 {
		t.Fatalf("deleted %v, another creator's session", d)
	}

	v.kg.mu.Lock()
	v.kg.sessions[old].Creator = v.kg.identity.PrincipalID()
	v.kg.deleteErr = []error{errors.New("rpc error: code = Unavailable")}
	v.kg.mu.Unlock()
	v.sweep()
	if d := v.kg.Deleted(); len(d) != 0 {
		t.Fatalf("deleted %v through a failed call", d)
	}
	if got := testutil.ToFloat64(v.m.TickErrors.WithLabelValues("sessions")); got != 1 {
		t.Fatalf("sessions phase errors = %v, want 1", got)
	}

	// kagent admitted an earlier delete that did not finish.
	v.kg.mu.Lock()
	v.kg.sessions[old].State = kagent.RuntimeDeleting
	v.kg.mu.Unlock()
	v.sweep()
	if d := v.kg.Deleted(); !slices.Equal(d, []string{old}) {
		t.Fatalf("deleted %v, want the replaced session %s", d, old)
	}
	if got := v.deletedFor(store.SessionReplaced); got != 1 {
		t.Fatalf("replaced deletions = %v, want 1", got)
	}
	if w := v.work(101); w.SessionID != "ctx-new" {
		t.Fatalf("work item session = %q, want it kept", w.SessionID)
	}
}

func TestSweepKeepsAStoppedWorkItemsSessionForADay(t *testing.T) {
	v := newDeliveryEnv(t)
	session := v.delivered()
	v.gh.close(1)
	v.poll()
	if w := v.work(101); w.State != store.WorkStopped {
		t.Fatalf("work item = %s, want stopped", w.State)
	}
	v.quiet()
	v.sweep()
	if d := v.kg.Deleted(); len(d) != 0 {
		t.Fatalf("deleted %v, a session a resumed run may still use", d)
	}
	if _, err := v.st.DB.Exec(`UPDATE work_item SET updated_at = updated_at - interval '25 hours' WHERE issue_id = 101`); err != nil {
		t.Fatal(err)
	}
	v.sweep()
	if d := v.kg.Deleted(); !slices.Equal(d, []string{session}) {
		t.Fatalf("deleted %v, want the stopped work item's session %s", d, session)
	}
	if w := v.work(101); w.SessionID != "" || w.State != store.WorkStopped {
		t.Fatalf("work item = %+v, want stopped with no session", w)
	}
	if got := v.deletedFor(store.SessionStopped); got != 1 {
		t.Fatalf("stopped deletions = %v, want 1", got)
	}
}

func TestSweepKeepsASessionWithAnOpenAttempt(t *testing.T) {
	v := newDeliveryEnv(t)
	session := v.delivered()
	v.gh.green("sha-1")
	v.poll()
	v.gh.merge(1, "merge-sha-1")
	v.poll()
	// A later run is still working in the session.
	v.its.add(2, "coder", 101, "HL-7", "Also tidy the helper.")
	v.clk.Advance(5 * time.Second)
	v.tick(v.e)
	v.quiet()
	v.sweep()
	if d := v.kg.Deleted(); len(d) != 0 {
		t.Fatalf("deleted %v during a turn", d)
	}
	if w := v.work(101); w.SessionID != session {
		t.Fatalf("work item session = %q, want %s kept", w.SessionID, session)
	}
}
