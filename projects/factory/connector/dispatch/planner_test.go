package dispatch_test

import (
	"strings"
	"testing"
)

func TestPlannerReconcilesLostBacklogCreate(t *testing.T) {
	v := newEnv(t)
	v.e.Release()
	v.cfg.Bindings[0].ItsaplanProject = "HOME"
	v.e = v.engine("planner-test")
	v.its.loseActivationCreate = true
	_, err := v.st.DB.Exec(`INSERT INTO planner_schedule(name,namespace,ceiling,min_interval,cooldown,per_run,issue_title,instructions)
 VALUES ('smoke','homelab:docs','backlog','1 hour','1 day',1,'Check documentation','Verify the documented claim.');
 SELECT admit_planner_run('smoke','r');
 UPDATE planner_run SET final_message='[{"title":"excerpt","body":"quoted","evidence":["https://example.org/doc"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:missing"}]' WHERE run_id='r'`)
	if err != nil {
		t.Fatal(err)
	}
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
	is := v.its.issues[1001]
	if is.Title != "Check documentation" || is.ColumnID != 5 || is.DelegateUserID != nil || !strings.Contains(is.Description, "<untrusted-evidence>") {
		t.Fatal("incorrect Backlog issue")
	}
}
