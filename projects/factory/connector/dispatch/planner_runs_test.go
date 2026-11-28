package dispatch_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

const plannerNS = "homelab:docs"

func plannerProposal(fp, fix string) string {
	return fmt.Sprintf(`{"title":"excerpt","body":"quoted","evidence":["https://example.org/doc"],"priority":"low","fix":%q,"fingerprint":%q}`, fix, fp)
}

// plannerRuntimeEnv is an engine with the planner runtime on, one reviewed
// schedule and the delegate ceiling.
func plannerRuntimeEnv(t *testing.T) *env {
	v := newEnv(t)
	v.e.Release()
	v.cfg.Bindings[0].ItsaplanProject = "HOME"
	v.cfg.Bindings[0].HumanGateAssignee = "joe"
	v.cfg.PlannerSchedules = []planner.Schedule{{
		Name: "smoke", Namespace: plannerNS, Ceiling: "delegate",
		MinInterval: time.Hour, Cooldown: 24 * time.Hour, PerRun: 2,
		IssueTitle: "Check documentation", Instructions: "Verify the documented claim.",
		Prompt: "Check the fixture evidence.",
	}}
	v.cfg.Planner = &dispatch.PlannerRuntime{
		AgentKey: "planner", Agent: kagent.AgentRef{Namespace: "factory-planner", Name: "planner"},
		RunTimeout: 30 * time.Minute,
	}
	v.e = v.engine("planner-runtime")
	return v
}

// cron queues a run as itsaplan's schedule does.
func (v *env) cron(id int64, prompt string) {
	v.its.mu.Lock()
	defer v.its.mu.Unlock()
	v.its.runs = append(v.its.runs, &fakeRun{
		run:   itsaplan.Run{ID: id, Trigger: "schedule", Prompt: prompt},
		agent: "planner", status: "pending", offsets: map[int]int{}, events: map[int][]itsaplan.Event{},
	})
}

func (v *env) plannerRows() int {
	var n int
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM planner_run`).Scan(&n); err != nil {
		v.t.Fatal(err)
	}
	return n
}

// endPlannerTask completes the newest planner task with a final message.
func (v *env) endPlannerTask(state, text string) {
	v.kg.finish(v.kg.lastTask(), state, text, nil)
}

func (v *env) lastResult(id int64) itsaplan.Result {
	v.t.Helper()
	rs := v.its.Results(id)
	if len(rs) == 0 {
		v.t.Fatalf("itsaplan run %d has no result", id)
	}
	return rs[len(rs)-1]
}

func TestPlannerRunAdmitsSendsFilesAndReports(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(900, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	sends := v.kg.Sends()
	if len(sends) != 1 || sends[0].agent != (kagent.AgentRef{Namespace: "factory-planner", Name: "planner"}) {
		t.Fatalf("sends = %+v", sends)
	}
	text := sends[0].msg.Parts[0].Text
	if !strings.HasPrefix(text, "Check the fixture evidence.") || !strings.Contains(text, "at most 2") || !strings.Contains(text, plannerNS+":") {
		t.Fatalf("planner message = %q", text)
	}
	if strings.Contains(text, planner.RunPromptPrefix) || sends[0].msg.ContextID == "" {
		t.Fatalf("itsaplan prompt reached the planner or the send had no fresh session: %+v", sends[0].msg)
	}
	v.endPlannerTask(kagent.StateCompleted, "["+plannerProposal(plannerNS+":file:missing", "agent")+","+plannerProposal(plannerNS+":file:stale", "human")+"]")
	v.tick(v.e)
	// One issue per tick: the agent fix goes to the coder, the human one to Joe.
	if v.its.activationCreates != 1 {
		t.Fatalf("issues after one tick = %d", v.its.activationCreates)
	}
	v.tick(v.e)
	if v.its.activationCreates != 2 {
		t.Fatalf("issues after two ticks = %d", v.its.activationCreates)
	}
	var delegated, joe int
	for _, is := range v.its.issues {
		if !strings.Contains(is.Description, planner.FingerprintMarker(plannerNS+":file:missing")) &&
			!strings.Contains(is.Description, planner.FingerprintMarker(plannerNS+":file:stale")) {
			t.Fatalf("issue lacks its fingerprint marker: %q", is.Description)
		}
		if is.DelegateUserID != nil && *is.DelegateUserID == "codex-user" {
			delegated++
		}
		if is.AssigneeUserID == "joe-id" && is.DelegateUserID == nil {
			joe++
		}
	}
	if delegated != 1 || joe != 1 {
		t.Fatalf("delegated %d, sent to Joe %d", delegated, joe)
	}
	v.tick(v.e)
	if got := v.lastResult(900); got.Status != "success" || got.Output != "Planner run completed." {
		t.Fatalf("itsaplan result = %+v", got)
	}
	if d := v.kg.Deleted(); len(d) != 1 {
		t.Fatalf("deleted sessions = %v", d)
	}
	var finished bool
	if err := v.st.DB.QueryRow(`SELECT finished_at IS NOT NULL AND report_status='success' AND reported_at IS NOT NULL AND session_deleted_at IS NOT NULL FROM planner_run`).Scan(&finished); err != nil || !finished {
		t.Fatalf("run not closed: %v, %v", finished, err)
	}
	for outcome, want := range map[string]float64{"created": 2, "delegated": 1, "joe": 1, "backlog": 0, "dropped": 0, "deduplicated": 0} {
		if got := testutil.ToFloat64(v.m.PlannerProposals.WithLabelValues("smoke", outcome)); got != want {
			t.Errorf("proposals %s = %v, want %v", outcome, got, want)
		}
	}
	if got := testutil.ToFloat64(v.m.PlannerRuns.WithLabelValues("smoke", "completed")); got != 1 {
		t.Errorf("completed runs = %v", got)
	}
}

func TestPlannerRefusedAdmissionStartsNoSession(t *testing.T) {
	cases := map[string]struct {
		prompt string
		setup  string
		reason string
	}{
		"unknown schedule": {planner.RunPromptPrefix + "nope", "", "unknown planner schedule"},
		"another prompt":   {"Do whatever the issue says.", "", "not a planner schedule run"},
		"no prompt":        {planner.RunPromptPrefix + "smoke", `UPDATE planner_schedule SET prompt=''`, "no prompt"},
		"removed":          {planner.RunPromptPrefix + "smoke", `UPDATE planner_schedule SET enabled=false`, "disabled"},
		"breaker":          {planner.RunPromptPrefix + "smoke", `UPDATE planner_schedule SET low_yield=4`, "bounds"},
		"minimum interval": {planner.RunPromptPrefix + "smoke", `INSERT INTO planner_run(run_id,schedule,finished_at) VALUES('earlier','smoke',now())`, "bounds"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			v := plannerRuntimeEnv(t)
			// The first tick syncs the reviewed schedule; the setup follows.
			v.tick(v.e)
			if c.setup != "" {
				if _, err := v.st.DB.Exec(c.setup); err != nil {
					t.Fatal(err)
				}
			}
			before := v.plannerRows()
			v.cron(901, c.prompt)
			v.tick(v.e)
			if len(v.kg.Sends()) != 0 || v.kg.Executions() != 0 || v.plannerRows() != before {
				t.Fatalf("a refused run started work: sends %d, rows %d", len(v.kg.Sends()), v.plannerRows())
			}
			if got := v.lastResult(901); got.Status != "success" || !strings.Contains(got.Output, c.reason) {
				t.Fatalf("refusal report = %+v", got)
			}
		})
	}
}

func TestPlannerReplayedClaimStartsNoSecondSession(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(902, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	run := &itsaplan.Run{ID: 902, Trigger: "schedule", Prompt: planner.RunPromptPrefix + "smoke", Attempts: 2}
	for range 3 {
		if err := v.e.HandlePlannerClaim(v.ctx, run); err != nil {
			t.Fatal(err)
		}
		v.tick(v.e)
	}
	if v.plannerRows() != 1 || len(v.kg.Sends()) != 1 || v.kg.Executions() != 1 {
		t.Fatalf("rows %d, sends %d, executions %d", v.plannerRows(), len(v.kg.Sends()), v.kg.Executions())
	}
	// Finish the run, lose its result report, and let itsaplan hand the run out
	// again: the stored run is reported once more and nothing starts.
	v.endPlannerTask(kagent.StateCompleted, "[]")
	v.tick(v.e)
	v.tick(v.e)
	v.its.mu.Lock()
	r := v.its.get(902)
	r.status, r.until = "pending", time.Time{}
	v.its.mu.Unlock()
	v.tick(v.e)
	v.tick(v.e)
	if v.plannerRows() != 1 || len(v.kg.Sends()) != 1 || v.its.get(902).status != "success" {
		t.Fatalf("rows %d, sends %d, status %s", v.plannerRows(), len(v.kg.Sends()), v.its.get(902).status)
	}
}

func TestPlannerLostSendResponseReconcilesWithoutResend(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.kg.loseResponse = true
	v.cron(903, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	var task string
	if err := v.st.DB.QueryRow(`SELECT coalesce(task_id,'') FROM planner_run`).Scan(&task); err != nil || task != "" {
		t.Fatalf("task recorded after a lost response: %q, %v", task, err)
	}
	v.tick(v.e)
	if err := v.st.DB.QueryRow(`SELECT coalesce(task_id,'') FROM planner_run`).Scan(&task); err != nil || task == "" {
		t.Fatalf("task not recovered by message ID: %q, %v", task, err)
	}
	if len(v.kg.Sends()) != 1 || v.kg.Executions() != 1 {
		t.Fatalf("sends %d, executions %d", len(v.kg.Sends()), v.kg.Executions())
	}
}

func TestPlannerDefiniteRefusalRetriesThenFails(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.kg.notAccepted = 2
	v.cron(904, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.tick(v.e)
	v.tick(v.e)
	if v.kg.Executions() != 1 {
		t.Fatalf("executions = %d", v.kg.Executions())
	}
	v2 := plannerRuntimeEnv(t)
	v2.kg.notAccepted = 99
	v2.cron(905, planner.RunPromptPrefix+"smoke")
	for range 8 {
		v2.tick(v2.e)
	}
	if got := v2.lastResult(905); got.Status != "failed" || !strings.Contains(got.Error, "did not accept") {
		t.Fatalf("result = %+v", got)
	}
}

func TestPlannerFailedTaskCountsLowYieldAndFreesTheSchedule(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(906, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.endPlannerTask(kagent.StateFailed, "boom")
	v.tick(v.e)
	v.tick(v.e)
	if got := v.lastResult(906); got.Status != "failed" || !strings.Contains(got.Error, "boom") {
		t.Fatalf("result = %+v", got)
	}
	var low int
	var finished bool
	if err := v.st.DB.QueryRow(`SELECT s.low_yield, r.finished_at IS NOT NULL FROM planner_schedule s, planner_run r`).Scan(&low, &finished); err != nil || low != 1 || !finished {
		t.Fatalf("low yield %d, finished %v: %v", low, finished, err)
	}
	if got := testutil.ToFloat64(v.m.PlannerRuns.WithLabelValues("smoke", "failed")); got != 1 {
		t.Fatalf("failed runs = %v", got)
	}
}

func TestPlannerTimeoutCancelsTheTask(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(907, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	if _, err := v.st.DB.Exec(`UPDATE planner_run SET started_at = now() - interval '2 hours'`); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	v.tick(v.e)
	if got := v.lastResult(907); got.Status != "failed" || !strings.Contains(got.Error, "timed out") {
		t.Fatalf("result = %+v", got)
	}
	if !v.kg.tasks[v.kg.lastTask()].cancelRequested {
		t.Fatal("the task was not canceled")
	}
}

func TestPlannerGoneItsaplanRunEndsTheSession(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(908, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.its.mu.Lock()
	v.its.get(908).status = "canceled"
	v.its.mu.Unlock()
	v.clk.Advance(2 * time.Minute)
	v.tick(v.e)
	var reason string
	if err := v.st.DB.QueryRow(`SELECT failure FROM planner_run`).Scan(&reason); err != nil || !strings.Contains(reason, "no longer active") {
		t.Fatalf("failure %q: %v", reason, err)
	}
	if !v.kg.tasks[v.kg.lastTask()].cancelRequested {
		t.Fatal("the task was not canceled")
	}
}

func TestPlannerBreakerTripsAndStaysTripped(t *testing.T) {
	v := plannerRuntimeEnv(t)
	for i := range 4 {
		id := int64(910 + i)
		v.cron(id, planner.RunPromptPrefix+"smoke")
		v.tick(v.e)
		v.endPlannerTask(kagent.StateCompleted, "[]")
		v.tick(v.e)
		v.tick(v.e)
		if _, err := v.st.DB.Exec(`UPDATE planner_run SET started_at = now() - interval '2 hours'`); err != nil {
			t.Fatal(err)
		}
	}
	var low int
	if err := v.st.DB.QueryRow(`SELECT low_yield FROM planner_schedule`).Scan(&low); err != nil || low != 4 {
		t.Fatalf("low yield %d: %v", low, err)
	}
	if got := testutil.ToFloat64(v.m.PlannerBreakerOpen.WithLabelValues("smoke")); got != 1 {
		t.Fatalf("breaker gauge = %v", got)
	}
	sends := len(v.kg.Sends())
	for i := range 2 {
		id := int64(920 + i)
		v.cron(id, planner.RunPromptPrefix+"smoke")
		v.tick(v.e)
		if len(v.kg.Sends()) != sends || !strings.Contains(v.lastResult(id).Output, "Not admitted") {
			t.Fatalf("a tripped breaker admitted run %d", id)
		}
	}
}

func TestPlannerCeilingBacklogCreatesNoDelegatedIssue(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cfg.PlannerSchedules[0].Ceiling = "backlog"
	v.e.Release()
	v.e = v.engine("planner-backlog")
	v.cron(930, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	// The planner asks for an agent fix and a human one. It cannot raise either.
	v.endPlannerTask(kagent.StateCompleted, "["+plannerProposal(plannerNS+":file:a", "agent")+","+plannerProposal(plannerNS+":file:b", "human")+"]")
	for range 4 {
		v.tick(v.e)
	}
	if v.its.activationCreates != 2 {
		t.Fatalf("issues = %d", v.its.activationCreates)
	}
	for _, is := range v.its.issues {
		if is.DelegateUserID != nil || is.AssigneeUserID != "" || is.ColumnID != 5 {
			t.Fatalf("the backlog ceiling raised an issue: %+v", is)
		}
	}
}

// plannerFiled runs one planner run to a filed issue for fingerprint fp.
func plannerFiled(v *env, id int64, fp, fix string) *itsaplan.Issue {
	v.t.Helper()
	v.cron(id, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.endPlannerTask(kagent.StateCompleted, "["+plannerProposal(fp, fix)+"]")
	v.tick(v.e)
	var key string
	if err := v.st.DB.QueryRow(`SELECT coalesce(issue_key,'') FROM planner_proposal WHERE fingerprint=$1`, fp).Scan(&key); err != nil || key == "" {
		v.t.Fatalf("proposal %s not filed: %q, %v", fp, key, err)
	}
	for _, is := range v.its.issues {
		if is.Identifier == key {
			return is
		}
	}
	v.t.Fatalf("issue %s missing", key)
	return nil
}

func TestPlannerDeduplicatesAgainstTheTracker(t *testing.T) {
	v := plannerRuntimeEnv(t)
	marked := plannerNS + ":file:marked"
	tracked := plannerNS + ":file:tracked"
	closedFP := plannerNS + ":file:closed"
	fresh := plannerNS + ":file:fresh"
	v.cfg.PlannerTracked = []planner.Tracked{{Fingerprint: tracked, Issue: "HOME-4"}}
	v.e.Release()
	v.e = v.engine("planner-dedupe")
	v.its.issues = map[int64]*itsaplan.Issue{
		50: {ID: 50, Identifier: "HOME-50", ColumnID: 1, Description: "Found by hand.\n" + planner.FingerprintMarker(marked)},
		4:  {ID: 4, Identifier: "HOME-4", ColumnID: 2, Description: "Scheduled job."},
		51: {ID: 51, Identifier: "HOME-51", ColumnID: 3, Description: planner.FingerprintMarker(closedFP)},
	}
	v.cron(940, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.endPlannerTask(kagent.StateCompleted, "["+plannerProposal(marked, "agent")+","+plannerProposal(tracked, "agent")+","+plannerProposal(closedFP, "agent")+","+plannerProposal(fresh, "agent")+"]")
	for range 3 {
		v.tick(v.e)
	}
	var dedup, dropped int
	if err := v.st.DB.QueryRow(`SELECT deduplicated, dropped FROM planner_run`).Scan(&dedup, &dropped); err != nil {
		t.Fatal(err)
	}
	// Two are covered by open issues. The Done issue covers nothing, so the
	// other two fit the run's limit of two.
	if dedup != 2 || dropped != 0 {
		t.Fatalf("deduplicated %d, dropped %d", dedup, dropped)
	}
	var fps []string
	rows, err := v.st.DB.Query(`SELECT fingerprint FROM planner_proposal ORDER BY fingerprint`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var fp string
		if err := rows.Scan(&fp); err != nil {
			t.Fatal(err)
		}
		fps = append(fps, fp)
	}
	rows.Close()
	if strings.Join(fps, ",") != closedFP+","+fresh {
		t.Fatalf("filed fingerprints = %v", fps)
	}
	if got := testutil.ToFloat64(v.m.PlannerProposals.WithLabelValues("smoke", "deduplicated")); got != 2 {
		t.Fatalf("deduplicated metric = %v", got)
	}
}

func TestPlannerOutcomesFeedCooldownAndBreaker(t *testing.T) {
	v := plannerRuntimeEnv(t)
	fp := plannerNS + ":file:one"
	is := plannerFiled(v, 950, fp, "agent")
	v.tick(v.e)
	v.tick(v.e)
	var low int
	if err := v.st.DB.QueryRow(`SELECT low_yield FROM planner_schedule`).Scan(&low); err != nil || low != 1 {
		t.Fatalf("low yield before the outcome = %d: %v", low, err)
	}
	// Done proves the finding and takes the run out of the breaker's streak.
	v.its.mu.Lock()
	v.its.issues[is.ID].ColumnID = 3
	v.its.mu.Unlock()
	v.clk.Advance(10 * time.Minute)
	v.tick(v.e)
	var reproducible, closed bool
	if err := v.st.DB.QueryRow(`SELECT reproducible, closed_at IS NOT NULL FROM planner_proposal`).Scan(&reproducible, &closed); err != nil || !reproducible || !closed {
		t.Fatalf("reproducible %v, closed %v: %v", reproducible, closed, err)
	}
	if err := v.st.DB.QueryRow(`SELECT low_yield FROM planner_schedule`).Scan(&low); err != nil || low != 0 {
		t.Fatalf("low yield after Done = %d: %v", low, err)
	}
}

func TestPlannerCanceledFixIsRejectedAndNotDelegatedAgainInCooldown(t *testing.T) {
	v := plannerRuntimeEnv(t)
	fp := plannerNS + ":file:two"
	is := plannerFiled(v, 951, fp, "agent")
	if is.DelegateUserID == nil {
		t.Fatal("the fix was not delegated")
	}
	v.tick(v.e)
	v.tick(v.e)
	v.its.mu.Lock()
	v.its.issues[is.ID].ColumnID = 4
	v.its.mu.Unlock()
	v.clk.Advance(10 * time.Minute)
	v.tick(v.e)
	var reproducible, closed bool
	if err := v.st.DB.QueryRow(`SELECT reproducible, closed_at IS NOT NULL FROM planner_proposal`).Scan(&reproducible, &closed); err != nil || reproducible || !closed {
		t.Fatalf("reproducible %v, closed %v: %v", reproducible, closed, err)
	}
	// The same fingerprint comes back inside its cooldown: nothing is filed.
	creates := v.its.activationCreates
	if _, err := v.st.DB.Exec(`UPDATE planner_run SET started_at = now() - interval '2 hours'`); err != nil {
		t.Fatal(err)
	}
	v.cron(952, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.endPlannerTask(kagent.StateCompleted, "["+plannerProposal(fp, "agent")+"]")
	for range 3 {
		v.tick(v.e)
	}
	if v.its.activationCreates != creates {
		t.Fatal("a rejected fingerprint was filed again inside its cooldown")
	}
}

func TestPlannerFailedDelegatedFixIsRejected(t *testing.T) {
	v := plannerRuntimeEnv(t)
	fp := plannerNS + ":file:three"
	is := plannerFiled(v, 953, fp, "agent")
	v.tick(v.e)
	v.tick(v.e)
	// The coder's run on the delegated issue ended failed.
	for _, q := range []string{
		fmt.Sprintf(`INSERT INTO run(id,agent_key,issue_id,issue_key,trigger,prompt,system_prompt,attempt,state) VALUES(7001,'coder',%d,'%s','delegation','p','s',1,'closed')`, is.ID, is.Identifier),
		`INSERT INTO outbox(kind,dedupe_key,run_id,payload) VALUES('result','result/7001/1',7001,'{"status":"failed","error":"x"}')`,
	} {
		if _, err := v.st.DB.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	v.clk.Advance(10 * time.Minute)
	v.tick(v.e)
	var reproducible, closed bool
	if err := v.st.DB.QueryRow(`SELECT reproducible, closed_at IS NOT NULL FROM planner_proposal`).Scan(&reproducible, &closed); err != nil || reproducible || !closed {
		t.Fatalf("reproducible %v, closed %v: %v", reproducible, closed, err)
	}
}

func TestPlannerTokensComeFromTheBrokerOrTaskMetadata(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(960, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	var ctxID string
	if err := v.st.DB.QueryRow(`SELECT context_id FROM planner_run`).Scan(&ctxID); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{
		fmt.Sprintf(`INSERT INTO codex_call(id,atespace,session_id,actor_uid) VALUES(1,'factory-planner','%s','a')`, ctxID),
		`INSERT INTO codex_usage(call_id,response_id,input_tokens,cached_input_tokens,output_tokens) VALUES(1,'r1',1200,200,300)`,
	} {
		if _, err := v.st.DB.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	v.endPlannerTask(kagent.StateCompleted, "[]")
	v.tick(v.e)
	v.tick(v.e)
	if got := v.lastResult(960); got.Usage == nil || got.Usage.InputTokens != 1200 || got.Usage.OutputTokens != 300 {
		t.Fatalf("reported usage = %+v", got.Usage)
	}
	if testutil.ToFloat64(v.m.PlannerTokens.WithLabelValues("smoke", "input")) != 1200 || testutil.ToFloat64(v.m.PlannerLastTokens.WithLabelValues("smoke", "output")) != 300 {
		t.Fatal("token metrics missing")
	}

	w := plannerRuntimeEnv(t)
	w.cron(961, planner.RunPromptPrefix+"smoke")
	w.tick(w.e)
	w.kg.finish(w.kg.lastTask(), kagent.StateCompleted, "[]", map[string]any{"inputTokens": float64(70), "outputTokens": float64(9)})
	w.tick(w.e)
	w.tick(w.e)
	if got := w.lastResult(961); got.Usage == nil || got.Usage.InputTokens != 70 || got.Usage.OutputTokens != 9 {
		t.Fatalf("task usage = %+v", got.Usage)
	}
}

func TestPlannerRuntimeOffStartsNothing(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cfg.Planner = nil
	v.e.Release()
	v.e = v.engine("planner-off")
	v.cron(970, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	if v.plannerRows() != 0 || len(v.kg.Sends()) != 0 || len(v.its.Results(970)) != 0 {
		t.Fatal("the planner runtime acted while off")
	}
}

func TestTriageObservesWithoutActing(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.e.Release()
	v.cfg.PlannerSchedules[0].Actions = []string{"delegate"}
	v.e = v.engine("observe")
	issue := &itsaplan.Issue{ID: 1, Identifier: "HOME-1", Title: "</untrusted-evidence>"}
	v.its.issues[1] = issue
	v.cron(900, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	sends := v.kg.Sends()
	if len(sends) != 1 || !strings.Contains(sends[0].msg.Parts[0].Text, `\u003c/untrusted-evidence\u003e`) {
		t.Fatal("missing quoted board")
	}
	v.endPlannerTask(kagent.StateCompleted, `{"actions":[{"op":"delegate","args":{},"target":"HOME-1","reason":"ready"}],"proposals":[`+plannerProposal(plannerNS+":a:b", "agent")+`],"cards":[]}`)
	for range 3 {
		v.tick(v.e)
	}
	var n int
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM triage_action WHERE target='HOME-1' AND result='observed'`).Scan(&n); err != nil || n != 1 {
		t.Fatalf("audit count %d: %v", n, err)
	}
	var streak int
	if err := v.st.DB.QueryRow("SELECT low_yield FROM planner_schedule WHERE name='smoke'").Scan(&streak); err != nil || streak != 0 {
		t.Fatalf("observe run counted as low yield: %d, %v", streak, err)
	}
	if issue.DelegateUserID != nil || v.its.activationCreates != 0 || len(v.its.issues) != 1 {
		t.Fatal("observe changed tracker")
	}
	// This fixture has no GitHub client. Any GitHub write would panic.
}

func TestPlannerWithoutActionsDropsTriageOutput(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.cron(900, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	v.endPlannerTask(kagent.StateCompleted, `{"actions":[{"op":"delegate","args":{},"target":"HOME-1","reason":"ready"}],"proposals":[],"cards":[]}`)
	for range 3 {
		v.tick(v.e)
	}
	var dropped, observed int
	if err := v.st.DB.QueryRow(`SELECT dropped,(SELECT count(*) FROM triage_action) FROM planner_run WHERE itsaplan_run_id=900`).Scan(&dropped, &observed); err != nil || dropped != 1 || observed != 0 {
		t.Fatalf("dropped %d, observed %d: %v", dropped, observed, err)
	}
}

func TestTriageResultProcessingNeedsNoExternalClient(t *testing.T) {
	v := plannerRuntimeEnv(t)
	v.e.Release()
	v.cfg.PlannerSchedules[0].Actions = []string{"delegate"}
	v.e = v.engine("offline")
	v.its.issues[1] = &itsaplan.Issue{ID: 1, Identifier: "HOME-1"}
	v.cron(900, planner.RunPromptPrefix+"smoke")
	v.tick(v.e)
	var reads int
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM triage_read JOIN planner_run USING(message_id) WHERE itsaplan_run_id=900 AND writer_epoch>0 AND attempts=1 AND operation IN ('board','project','issue')`).Scan(&reads); err != nil || reads != 3 {
		t.Fatalf("journal rows %d: %v", reads, err)
	}
	_, err := v.st.DB.Exec(`UPDATE planner_run SET final_message='{"actions":[{"op":"delegate","args":{},"target":"HOME-1","reason":"ready"}],"proposals":[],"cards":[]}' WHERE itsaplan_run_id=900`)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := v.e.ConsumePlannerResultOffline(v.ctx); err != nil {
			t.Fatal(err)
		}
	}
	var observed int
	if err := v.st.DB.QueryRow(`SELECT count(*) FROM triage_action JOIN planner_run USING(run_id) WHERE itsaplan_run_id=900 AND result='observed' AND processed`).Scan(&observed); err != nil || observed != 1 {
		t.Fatalf("observed %d: %v", observed, err)
	}
}
