package dispatch_test

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

func plannerFixture(t *testing.T) *env {
	v := newEnv(t)
	v.e.Release()
	v.cfg.Bindings[0].ItsaplanProject = "HOME"
	v.e = v.engine("planner-test")
	_, err := v.st.DB.Exec(`INSERT INTO planner_schedule(name,namespace,ceiling,min_interval,cooldown,per_run,issue_title,instructions)
 VALUES ('smoke','homelab:docs','backlog','1 hour','1 day',1,'Check documentation','Verify the documented claim.');
 SELECT admit_planner_run('smoke','r');
 UPDATE planner_run SET final_message='[{"title":"excerpt","body":"quoted","evidence":["https://example.org/doc"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:missing"}]' WHERE run_id='r'`)
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
