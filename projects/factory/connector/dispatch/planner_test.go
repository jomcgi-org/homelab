// invariant: FACT-14
package dispatch_test

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
)

func plannerFixture(t *testing.T) *env {
	v := newEnv(t)
	v.e.Release()
	v.cfg.Bindings[0].ItsaplanProject = "HOME"
	v.cfg.PlannerSchedules = []planner.Schedule{{
		Name: "smoke", Namespace: "homelab:docs", Ceiling: "unknown",
		MinInterval: time.Hour, Cooldown: 24 * time.Hour, PerRun: 1,
		IssueTitle: "Check documentation", Instructions: "Verify the documented claim.",
	}}
	v.e = v.engine("planner-test")
	var admitted bool
	if err := v.st.DB.QueryRow("SELECT admit_planner_run('smoke','r')").Scan(&admitted); err != nil || !admitted {
		t.Fatalf("planner admission = %v, %v", admitted, err)
	}
	_, err := v.st.DB.Exec(`UPDATE planner_run SET final_message='[{"title":"excerpt","body":"quoted","evidence":["https://example.org/doc"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:missing"}]' WHERE run_id='r'`)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestPlannerReconcilesLostBacklogCreate(t *testing.T) {
	v := plannerFixture(t)
	v.its.activationCreateErr = &itsaplan.HTTPError{Status: 403}
	v.tick(v.e)
	v.its.activationCreateErr = nil
	if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
		t.Fatal(err)
	}
	v.its.loseActivationCreate = true
	v.tick(v.e)
	if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	v.tick(v.e)
	var key string
	if err := v.st.DB.QueryRow("SELECT issue_key FROM planner_proposal WHERE run_id='r'").Scan(&key); err != nil {
		t.Fatal(err)
	}
	if key != "HOME-1001" || v.its.activationCreates != 1 {
		t.Fatalf("receipt %q, creates %d", key, v.its.activationCreates)
	}
	var reads int
	if err := v.st.DB.QueryRow("SELECT attempts FROM planner_tracker_attempt WHERE operation='GetIssue' AND resource='1001' AND writer_epoch>0").Scan(&reads); err != nil || reads != 1 {
		t.Fatalf("candidate attempts %d, error %v", reads, err)
	}
	is := v.its.issues[1001]
	if is.Title != "Check documentation" || is.ColumnID != 5 || is.DelegateUserID != nil || !strings.Contains(is.Description, "<untrusted-evidence>") {
		t.Fatal("incorrect Backlog issue")
	}
}

func TestPlannerPersistentCreateRefusalReleasesRun(t *testing.T) {
	v := plannerFixture(t)
	v.its.activationCreateErr = &itsaplan.HTTPError{Status: 403}
	for range 14 {
		v.tick(v.e)
		if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
			t.Fatal(err)
		}
	}
	var attempts int
	if err := v.st.DB.QueryRow(`SELECT a.attempts FROM planner_tracker_attempt a JOIN planner_run r USING(run_id)
 WHERE operation='CreateIssue' AND finished_at IS NOT NULL`).Scan(&attempts); err != nil || attempts != 12 {
		t.Fatalf("create attempts %d, error %v", attempts, err)
	}
}

func TestPlannerReceiptAfterCredentialRecovery(t *testing.T) {
	v := plannerFixture(t)
	v.its.loseActivationCreate = true
	v.tick(v.e)
	v.its.issueErr = &itsaplan.HTTPError{Status: 403}
	for range 12 {
		if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
			t.Fatal(err)
		}
		v.tick(v.e)
	}
	v.tick(v.e)
	var held bool
	if err := v.st.DB.QueryRow(`SELECT finished_at IS NOT NULL AND filing_stopped AND create_started AND closed_at IS NULL AND issue_key IS NULL AND next_attempt>now()+interval '50 minutes' FROM planner_proposal JOIN planner_run USING(run_id)`).Scan(&held); err != nil || !held {
		t.Fatalf("pending receipt after refusals: %v, %v", held, err)
	}
	v.e.Release()
	v.e = v.engine("recovered-writer")
	v.its.issueErr = nil
	if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	var key string
	if err := v.st.DB.QueryRow("SELECT issue_key FROM planner_proposal WHERE NOT filing_stopped").Scan(&key); err != nil || key != "HOME-1001" || v.its.activationCreates != 1 {
		t.Fatalf("recovered %q after %d creates: %v", key, v.its.activationCreates, err)
	}
}

func TestPlannerFilesOneIssuePerTickFromScheduleText(t *testing.T) {
	v := plannerFixture(t)
	_, err := v.st.DB.Exec(`UPDATE planner_schedule SET per_run=2;
 UPDATE planner_run SET final_message='[{"title":"first","body":"one","evidence":["https://example.org/1"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:one"},
 {"title":"second","body":"two","evidence":["https://example.org/2"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:two"}]' WHERE run_id='r'`)
	if err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	if v.its.activationCreates != 1 {
		t.Fatalf("first tick created %d issues", v.its.activationCreates)
	}
	is := v.its.issues[1001]
	if is.Title != "Check documentation" || !strings.HasPrefix(is.Description, "Verify the documented claim.\n\n<untrusted-evidence>\n> ") ||
		!strings.Contains(is.Description, "</untrusted-evidence>") {
		t.Fatalf("issue not built from the schedule: %q %q", is.Title, is.Description)
	}
	v.tick(v.e)
	if v.its.activationCreates != 2 {
		t.Fatalf("second tick left %d issues", v.its.activationCreates)
	}
}

func TestPlannerInconclusiveCreateReleasesSchedule(t *testing.T) {
	v := plannerFixture(t)
	v.its.activationCreateErr = errors.New("create timed out before commit")
	v.tick(v.e)
	v.its.activationCreateErr = nil
	for range 12 {
		if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
			t.Fatal(err)
		}
		v.tick(v.e)
	}
	v.tick(v.e)
	var held bool
	if err := v.st.DB.QueryRow(`SELECT finished_at IS NOT NULL AND filing_stopped AND create_started AND closed_at IS NULL AND issue_key IS NULL
 FROM planner_proposal JOIN planner_run USING(run_id)`).Scan(&held); err != nil || !held {
		t.Fatalf("schedule still pinned: %v, %v", held, err)
	}
	var admitted bool
	if err := v.st.DB.QueryRow(`UPDATE planner_run SET started_at=now()-interval '2 hours' WHERE run_id='r';
 SELECT admit_planner_run('smoke','next')`).Scan(&admitted); err != nil || !admitted || v.its.activationCreates != 0 {
		t.Fatalf("next run admitted %v after %d creates: %v", admitted, v.its.activationCreates, err)
	}
}

func TestPlannerFiledIssueResetsBreaker(t *testing.T) {
	v := plannerFixture(t)
	v.tick(v.e)
	v.tick(v.e)
	var streak int
	if err := v.st.DB.QueryRow(`SELECT low_yield FROM planner_schedule JOIN planner_run r ON r.schedule=name WHERE r.finished_at IS NOT NULL`).Scan(&streak); err != nil || streak != 1 {
		t.Fatalf("streak %d before the outcome: %v", streak, err)
	}
	var marked bool
	if err := v.st.DB.QueryRow("SELECT mark_planner_reproducible('HOME-1001')").Scan(&marked); err != nil || !marked {
		t.Fatalf("mark failed: %v", err)
	}
	if err := v.st.DB.QueryRow(`SELECT low_yield FROM planner_schedule s WHERE name='smoke' AND
 EXISTS (SELECT FROM planner_proposal WHERE reproducible AND issue_key='HOME-1001')`).Scan(&streak); err != nil || streak != 0 {
		t.Fatalf("streak %d after a reproducible filing: %v", streak, err)
	}
}

func TestPlannerDropsOversizeEvidence(t *testing.T) {
	v := plannerFixture(t)
	link := `"https://example.org/` + strings.Repeat("a", 1000) + `"`
	links := strings.TrimSuffix(strings.Repeat(link+",", 8), ",")
	_, err := v.st.DB.Exec(`UPDATE planner_run SET final_message=$1 WHERE run_id='r'`,
		`[{"title":"excerpt","body":"quoted","evidence":[`+links+`],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:missing"}]`)
	if err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	var dropped, proposals int
	if err := v.st.DB.QueryRow(`SELECT dropped,(SELECT count(*) FROM planner_proposal) FROM planner_run WHERE run_id='r'`).Scan(&dropped, &proposals); err != nil || dropped != 1 || proposals != 0 || v.its.activationCreates != 0 {
		t.Fatalf("dropped %d, proposals %d, creates %d: %v", dropped, proposals, v.its.activationCreates, err)
	}
}

func TestPlannerScheduleAdmissionAndReplay(t *testing.T) {
	v := plannerFixture(t)
	run := itsaplan.Run{ID: 61, Trigger: "schedule", Prompt: "smoke", Attempts: 1}
	claim := func() {
		t.Helper()
		if err := v.e.HandleClaim(v.ctx, "homelab-planner", &run); err != nil {
			t.Fatal(err)
		}
	}
	claim() // The fixture already has an active smoke run.
	if len(v.kg.sends) != 0 {
		t.Fatal("refused run started a session")
	}
	if _, err := v.st.DB.Exec("DELETE FROM planner_run WHERE run_id='r'"); err != nil {
		t.Fatal(err)
	}
	run.ID++
	claim()
	claim()
	if len(v.kg.sends) != 1 {
		t.Fatalf("got %d sends", len(v.kg.sends))
	}
	sent := v.kg.sends[0]
	if sent.agent != (kagent.AgentRef{Name: "homelab-planner", Namespace: "factory-homelab-planner"}) || sent.msg.ContextID != "" {
		t.Fatal("expected fresh planner session")
	}
	v.kg.finish(v.kg.order[0], kagent.StateCompleted, "[]", nil)
	run.Attempts++
	claim()
	var result string
	if err := v.st.DB.QueryRow("SELECT final_message FROM planner_run WHERE run_id='planner-62'").Scan(&result); err != nil || result != "[]" || len(v.kg.sends) != 1 {
		t.Fatalf("result %q, sends %d: %v", result, len(v.kg.sends), err)
	}
}

func TestPlannerStopAfterDispatch(t *testing.T) {
	v := plannerFixture(t)
	if _, err := v.st.DB.Exec("DELETE FROM planner_run WHERE run_id='r'"); err != nil {
		t.Fatal(err)
	}
	run := itsaplan.Run{ID: 62, Trigger: "schedule", Prompt: "smoke"}
	advance := func() {
		t.Helper()
		run.Attempts++
		if err := v.e.HandleClaim(v.ctx, "homelab-planner", &run); err != nil {
			t.Fatal(err)
		}
	}
	advance()
	// Simulate losing the acknowledgement after the task started.
	if _, err := v.st.DB.Exec(`UPDATE planner_run SET task_id=NULL,session_id=NULL WHERE run_id='planner-62';
 UPDATE run SET stopped_reason='operator stop' WHERE id=62`); err != nil {
		t.Fatal(err)
	}
	advance()
	advance()
	task := v.kg.order[0]
	if !v.kg.tasks[task].cancelRequested {
		t.Fatal("planner task not canceled")
	}
	check := func(want bool) {
		t.Helper()
		var finished bool
		if err := v.st.DB.QueryRow("SELECT processed AND finished_at IS NOT NULL AND final_message IS NULL FROM planner_run WHERE run_id='planner-62'").Scan(&finished); err != nil || finished != want {
			t.Fatalf("finished=%v want %v: %v", finished, want, err)
		}
	}
	check(false)
	var journaled bool
	if err := v.st.DB.QueryRow("SELECT cancel_request_id='planner-62/cancel' AND cancel_writer_epoch IS NOT NULL AND cancel_attempts=1 FROM planner_run WHERE run_id='planner-62'").Scan(&journaled); err != nil || !journaled {
		t.Fatalf("planner cancellation journal: %v, %v", journaled, err)
	}
	v.kg.finish(task, kagent.StateCanceled, "", nil)
	advance()
	check(true)
	var listReads, taskReads int
	if err := v.st.DB.QueryRow("SELECT (SELECT attempts FROM status_read WHERE message_id='planner-62/list/'), (SELECT attempts FROM status_read WHERE message_id=$1)", "planner-62/task/"+task).Scan(&listReads, &taskReads); err != nil || listReads != 1 || taskReads != 2 {
		t.Fatalf("planner reads: list=%d task=%d err=%v", listReads, taskReads, err)
	}
}
