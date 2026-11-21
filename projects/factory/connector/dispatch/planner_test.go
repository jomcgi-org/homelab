package dispatch_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
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

func TestPlannerOversizeEvidence(t *testing.T) {
	for _, match := range []string{"novel", "seed", "open", "retained"} {
		t.Run(match, func(t *testing.T) {
			v := plannerFixture(t)
			fp := "homelab:docs:file:missing"
			switch match {
			case "seed":
				fp = "homelab:docs:home-4:scheduled-agent"
			case "open":
				v.its.issues = map[int64]*itsaplan.Issue{44: {ID: 44, Identifier: "HOME-44", ColumnID: 5, Description: "<!-- factory-planner-fingerprint-e7d26b91e6b4eb4aa83beacb2b99f0936c64bf86e3ea57a1e65717aa04158b88 -->"}}
			case "retained":
				if _, err := v.st.DB.Exec(`INSERT INTO planner_run(run_id,schedule,finished_at) VALUES('old','smoke',now());
 INSERT INTO planner_proposal(run_id,fingerprint,disposition,closed_at) VALUES('old','homelab:docs:file:missing','delegate',now())`); err != nil {
					t.Fatal(err)
				}
			}
			link := `"https://example.org/` + strings.Repeat("a", 1000) + `"`
			links := strings.TrimSuffix(strings.Repeat(link+",", 8), ",")
			_, err := v.st.DB.Exec(`UPDATE planner_run SET final_message=$1 WHERE run_id='r'`,
				`[{"title":"excerpt","body":"quoted","evidence":[`+links+`],"priority":"low","fix":"agent","fingerprint":"`+fp+`"}]`)
			if err != nil {
				t.Fatal(err)
			}
			v.tick(v.e)
			var dropped, duplicates, proposals int
			if err := v.st.DB.QueryRow(`SELECT dropped,deduplicated,(SELECT count(*) FROM planner_proposal WHERE run_id='r') FROM planner_run WHERE run_id='r'`).Scan(&dropped, &duplicates, &proposals); err != nil {
				t.Fatal(err)
			}
			wantDropped := 0
			if match == "novel" {
				wantDropped = 1
			}
			if dropped != wantDropped || duplicates != 1-wantDropped || proposals != 0 || v.its.activationCreates != 0 {
				t.Fatalf("dropped %d duplicates %d reservations %d creates %d", dropped, duplicates, proposals, v.its.activationCreates)
			}
		})
	}
}

func (f *fakeItsaplan) GetIssueByNumber(ctx context.Context, project string, n int64) (*itsaplan.Issue, error) {
	for _, is := range f.issues {
		if is.Identifier == fmt.Sprintf("%s-%d", project, n) {
			return f.GetIssue(ctx, is.ID)
		}
	}
	return nil, itsaplan.ErrNotFound
}

func TestPlannerOpenFingerprint(t *testing.T) {
	v := plannerFixture(t)
	v.its.issues = map[int64]*itsaplan.Issue{44: {ID: 44, Identifier: "HOME-44", ColumnID: 5, Description: "<!-- factory-planner-fingerprint-e7d26b91e6b4eb4aa83beacb2b99f0936c64bf86e3ea57a1e65717aa04158b88 -->"}}
	v.tick(v.e)
	var duplicates int
	if err := v.st.DB.QueryRow("SELECT deduplicated FROM planner_run WHERE run_id='r'").Scan(&duplicates); err != nil || duplicates != 1 || v.its.activationCreates != 0 {
		t.Fatalf("duplicates %d creates %d: %v", duplicates, v.its.activationCreates, err)
	}
}

func TestPlannerFiledOutcomes(t *testing.T) {
	for _, outcome := range []string{"Done", "failed", "canceled"} {
		t.Run(outcome, func(t *testing.T) {
			v := plannerFixture(t)
			v.tick(v.e)
			v.tick(v.e)
			is := v.its.issues[1001]
			if !strings.Contains(is.Description, "<!-- factory-planner-fingerprint-e7d26b91e6b4eb4aa83beacb2b99f0936c64bf86e3ea57a1e65717aa04158b88 -->") {
				t.Fatal("missing fingerprint")
			}
			_, after, found := strings.Cut(is.Description, "</untrusted-evidence>")
			if !found || strings.Contains(after, "homelab:docs:file:missing") {
				t.Fatal("raw fingerprint escaped evidence block")
			}
			if outcome == "Done" {
				is.ColumnID = 3
			} else {
				v.its.activationRunPages = map[int64]*itsaplan.IssueRunPage{0: {Items: []itsaplan.IssueRun{{ID: 50, Username: "homelab-codex", Trigger: "delegation", Status: outcome}}}}
			}
			if _, err := v.st.DB.Exec("UPDATE planner_proposal SET disposition='delegate',next_attempt=now()"); err != nil {
				t.Fatal(err)
			}
			if outcome != "Done" {
				page := v.its.activationRunPages[0]
				old := page.Items[0]
				page.Items = []itsaplan.IssueRun{{ID: 51, Username: "homelab-codex", Trigger: "delegation", Status: "pending"}, old}
				v.tick(v.e)
				var open bool
				if err := v.st.DB.QueryRow("SELECT closed_at IS NULL FROM planner_proposal").Scan(&open); err != nil || !open {
					t.Fatalf("pending retry lost to older failure: %v", err)
				}
				page.Items[0].Status = outcome
				if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
					t.Fatal(err)
				}
			}
			v.tick(v.e)
			var closed, proven bool
			if err := v.st.DB.QueryRow("SELECT closed_at IS NOT NULL,reproducible FROM planner_proposal WHERE issue_key='HOME-1001'").Scan(&closed, &proven); err != nil || !closed || proven != (outcome == "Done") {
				t.Fatalf("closed %v reproducible %v: %v", closed, proven, err)
			}
			// Remove the tracker-side match to exercise the retained database row.
			archived := time.Now()
			is.ArchivedAt = &archived
			if _, err := v.st.DB.Exec(`INSERT INTO planner_run(run_id,schedule,final_message) VALUES('repeat','smoke','[{"title":"excerpt","body":"quoted","evidence":["https://example.org/doc"],"priority":"low","fix":"agent","fingerprint":"homelab:docs:file:missing"}]')`); err != nil {
				t.Fatal(err)
			}
			v.tick(v.e)
			var duplicates int
			if err := v.st.DB.QueryRow("SELECT deduplicated FROM planner_run WHERE run_id='repeat'").Scan(&duplicates); err != nil || duplicates != 1 || v.its.activationCreates != 1 {
				t.Fatalf("retained repeat: duplicates %d creates %d: %v", duplicates, v.its.activationCreates, err)
			}
			is.ArchivedAt = nil
			if outcome != "Done" {
				is.ColumnID = 3
				if _, err := v.st.DB.Exec("UPDATE planner_proposal SET next_attempt=now()"); err != nil {
					t.Fatal(err)
				}
				v.tick(v.e)
				var credited bool
				if err := v.st.DB.QueryRow("SELECT reproducible AND low_yield=1 FROM planner_proposal JOIN planner_run USING(run_id) JOIN planner_schedule ON name=schedule").Scan(&credited); err != nil || !credited {
					t.Fatalf("late Done credit %v: %v", credited, err)
				}
			}
		})
	}
}

func TestPlannerExistingIdeas(t *testing.T) {
	for n := 4; n <= 11; n++ {
		v := plannerFixture(t)
		fp := fmt.Sprintf("homelab:docs:home-%d:scheduled-agent", n)
		if _, err := v.st.DB.Exec("UPDATE planner_run SET final_message=replace(final_message,'homelab:docs:file:missing',$1)", fp); err != nil {
			t.Fatal(err)
		}
		v.tick(v.e)
		var count int
		if err := v.st.DB.QueryRow("SELECT deduplicated FROM planner_run WHERE run_id='r'").Scan(&count); err != nil || count != 1 || v.its.activationCreates != 0 {
			t.Fatalf("HOME-%d duplicates %d: %v", n, count, err)
		}
	}
}

func TestPlannerFingerprintReadRefusal(t *testing.T) {
	v := plannerFixture(t)
	v.its.issues = map[int64]*itsaplan.Issue{44: {ID: 44, Identifier: "HOME-44", ColumnID: 5, Description: "<!-- factory-planner-fingerprint-e7d26b91e6b4eb4aa83beacb2b99f0936c64bf86e3ea57a1e65717aa04158b88 -->"}}
	v.its.issueErr = &itsaplan.HTTPError{Status: 403}
	for range 13 {
		v.tick(v.e)
		if _, err := v.st.DB.Exec("UPDATE planner_run SET lookup_after=now()"); err != nil {
			t.Fatal(err)
		}
	}
	var attempts int
	if err := v.st.DB.QueryRow(`SELECT attempts FROM planner_tracker_attempt JOIN planner_run USING(run_id) WHERE operation='LookupIssue' AND writer_epoch>0 AND dropped=1 AND finished_at IS NOT NULL`).Scan(&attempts); err != nil || attempts != 12 || v.its.activationCreates != 0 {
		t.Fatalf("attempts %d creates %d: %v", attempts, v.its.activationCreates, err)
	}
}
