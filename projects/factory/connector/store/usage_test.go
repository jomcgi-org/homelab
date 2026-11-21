package store_test

import (
	"database/sql"
	_ "embed"
	"reflect"
	"testing"
)

// Execute the same query documented for operators, so filtering and totals
// cannot silently drift away from the tested report.
//
//go:embed delivered_issue_usage.sql
var deliveredIssueUsage string

func TestDeliveredIssueUsage(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory', 'coder', 'factory/', 2);
		INSERT INTO work_item (id, binding_name, issue_id, issue_key, branch, agent_key,
			kagent_namespace, kagent_agent, state)
		VALUES (1, 'homelab', 101, 'HOME-101', 'factory/HOME-101', 'coder', 'factory', 'coder', 'done'),
		       (2, 'homelab', 102, 'HOME-102', 'factory/HOME-102', 'coder', 'factory', 'coder', 'done'),
		       (3, 'homelab', 103, 'HOME-103', 'factory/HOME-103', 'coder', 'factory', 'coder', 'active'),
		       (4, 'homelab', 104, 'HOME-104', 'factory/HOME-104', 'coder', 'factory', 'coder', 'review'),
		       (5, 'homelab', 105, 'HOME-105', 'factory/HOME-105', 'coder', 'factory', 'coder', 'stopped'),
		       (6, 'homelab', 106, 'HOME-106', 'factory/HOME-106', 'coder', 'factory', 'coder', 'done'),
		       (7, 'homelab', 107, 'HOME-107', 'factory/HOME-107', 'coder', 'factory', 'coder', 'done'),
		       (8, 'homelab', 108, 'HOME-108', 'factory/HOME-108', 'coder', 'factory', 'coder', 'done'),
		       (9, 'homelab', 109, 'HOME-109', 'factory/HOME-109', 'coder', 'factory', 'coder', 'done');
		INSERT INTO run (id, work_item_id, agent_key, trigger, prompt, system_prompt, attempt,
			state, closed_reason, input_tokens, cached_input_tokens, output_tokens)
		VALUES (1, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 100, 50, 10),
		       (2, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'failed', 200, 100, 20),
		       (-1, 1, 'coder', 'review', '', '', 1, 'closed', 'success', 300, 150, 30),
		       (3, 1, 'coder', 'issue_delegated', '', '', 1, 'closed', 'canceled', NULL, NULL, NULL),
		       (4, 2, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 400, 200, 40),
		       (5, 3, 'coder', 'issue_delegated', '', '', 1, 'dispatched', NULL, 9000, 0, 900),
		       (6, 4, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (7, 5, 'coder', 'issue_delegated', '', '', 1, 'closed', 'canceled', 9000, 0, 900),
		       (8, 6, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', NULL, NULL, NULL),
		       (9, 7, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (10, 8, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900),
		       (11, 9, 'coder', 'issue_delegated', '', '', 1, 'closed', 'success', 9000, 0, 900);
		UPDATE run SET origin = 'connector' WHERE id = -1;
		INSERT INTO outbox (kind, dedupe_key, payload, delivered_at, abandoned_at)
		VALUES ('issue_done', 'issue-done/1', '{"issueId":101}', now(), NULL),
		       ('issue_done', 'issue-done/2', '{"issueId":102}', now(), NULL),
		       ('issue_done', 'issue-done/6', '{"issueId":106}', now(), NULL),
		       ('issue_done', 'issue-done/7', '{"issueId":107}', NULL, NULL),
		       ('issue_done', 'issue-done/8', '{"issueId":108}', NULL, now()),
		       ('comment', 'comment/merged/9', '{"issueId":109}', now(), NULL);
	`)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := st.DB.Query(deliveredIssueUsage)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	type report struct {
		issue                        string
		runs, unmeasured             int64
		input, cached, output, total sql.NullInt64
	}
	var got []report
	for rows.Next() {
		var r report
		if err := rows.Scan(&r.issue, &r.runs, &r.unmeasured, &r.input, &r.cached, &r.output, &r.total); err != nil {
			t.Fatal(err)
		}
		got = append(got, r)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	measured := func(n int64) sql.NullInt64 { return sql.NullInt64{Int64: n, Valid: true} }
	want := []report{
		{issue: "HOME-101", runs: 4, unmeasured: 1, input: measured(600), cached: measured(300), output: measured(60), total: measured(660)},
		{issue: "HOME-102", runs: 1, input: measured(400), cached: measured(200), output: measured(40), total: measured(440)},
		{issue: "HOME-106", runs: 1, unmeasured: 1},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("delivered-issue report = %+v, want %+v", got, want)
	}
}

func TestPRDeliveryResults(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
	INSERT INTO repo_binding(name,owner,repo,base_branch,itsaplan_project,kagent_namespace,kagent_agent,branch_prefix,max_engineering_retries)
	VALUES ('b','org','repo','main','HOME','ns','coder','factory/',1);
	INSERT INTO work_item(id,binding_name,issue_id,issue_key,branch,agent_key,kagent_namespace,kagent_agent)
	SELECT i,'b',i,'HOME-'||i,'branch'||i,'coder','ns','coder' FROM generate_series(1,2) i;
	INSERT INTO run(id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,arm,task_class,base_sha,input_tokens,output_tokens)
	SELECT i,i,'coder','issue_delegated','','',1,'closed','control','task','base',100,20 FROM generate_series(1,2) i;
	INSERT INTO run(id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,arm,input_tokens,output_tokens,created_at)
	VALUES (-1,1,'coder','review','','',1,'closed','revision',20,10,now()+interval '1 minute');
	INSERT INTO pull_request(work_item_id,number,node_id,head_sha,state)
	SELECT i,i,'pr'||i,'head','open' FROM generate_series(1,2) i;
	INSERT INTO delivery_attempt(run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,
        kagent_namespace,kagent_agent,state,writer_epoch,first_sent_at)
    VALUES (2,1,1,1,'decision','initial','msg','ns','coder','accepted',1,now()-interval '17 minutes');
    INSERT INTO delivery_attempt(run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,
        kagent_namespace,kagent_agent,state,writer_epoch,first_sent_at,finished_at)
    VALUES (1,1,1,1,'first','initial','first','ns','coder','completed',1,now()-interval '10 minutes',now()),
           (-1,1,1,1,'revision','continuation','revision','ns','coder','completed',1,now()-interval '15 minutes',now());
    INSERT INTO review_event(pull_request_id,github_node_id,commit_sha,author,kind,source,disposition,round_run_id)
    SELECT id,'event','head','reviewer','changes_requested','reviewer','acted',-1 FROM pull_request WHERE number=1;
    UPDATE pull_request SET state='closed' WHERE number=2;
 UPDATE work_item SET state='stopped' WHERE id=2;
	UPDATE pull_request SET state='merged',merged_at=now()-interval '8 days',merge_commit_sha='sha' WHERE work_item_id=1;
	UPDATE delivery_outcome SET reverted_at=merged_at+interval '6 days' WHERE number=1;`)
	if err != nil {
		t.Fatal(err)
	}
	var ok bool
	err = st.DB.QueryRow(`SELECT n=2 AND merge_rate=0.5 AND revert_rate=1 AND tokens_per_merge=270
        AND median_coder_minutes_per_merge=25 AND median_review_rounds=0.5
        AND (SELECT first_build_minutes=10 AND rework_minutes=15 AND review_rounds=1
             FROM delivery_outcome WHERE node_id='pr1')
		FROM arm_delivery_results WHERE arm='control'`).Scan(&ok)
	if err != nil || !ok {
		t.Fatalf("unexpected arm results: %v %v", ok, err)
	}
	if _, err := st.DB.Exec(`UPDATE delivery_outcome SET reverted_at=merged_at+interval '8 days' WHERE number=1`); err == nil {
		t.Fatal("accepted revert outside observation window")
	}

	_, err = st.DB.Exec(`UPDATE work_item SET state='active' WHERE id=2;
    UPDATE pull_request SET node_id='replacement',number=3,state='open' WHERE work_item_id=2;
    INSERT INTO run(id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,input_tokens,output_tokens)
    VALUES(4,2,'coder','review','','',1,'closed',40,10);
    UPDATE pull_request SET merged_at=now(),state='merged' WHERE number=3;
    UPDATE delivery_attempt SET finished_at=first_sent_at+interval '17 minutes',state='canceled'
    WHERE message_id='msg';`)
	if err != nil {
		t.Fatal(err)
	}
	err = st.DB.QueryRow(`SELECT n=3 AND tokens_per_merge=160 AND (SELECT tokens=120 AND first_build_minutes=17 AND rework_minutes=0 FROM delivery_outcome WHERE number=2)
        FROM arm_delivery_results WHERE arm='control'`).Scan(&ok)
	if err != nil || !ok {
		t.Fatalf("replacement attribution: %v, %v", ok, err)
	}

	// A work item that stops before it opens a PR is not in the cohort.
	_, err = st.DB.Exec(`
        INSERT INTO work_item(id,binding_name,issue_id,issue_key,branch,agent_key,kagent_namespace,kagent_agent)
        VALUES(3,'b',3,'HOME-3','branch3','coder','ns','coder'),(4,'b',4,'HOME-4','branch4','coder','ns','coder');
        INSERT INTO run(id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,arm,task_class,base_sha)
        VALUES(5,3,'coder','issue_delegated','','',1,'closed','candidate','feature','guide2'),
              (6,4,'coder','issue_delegated','','',1,'closed','candidate','feature','guide2');
        UPDATE work_item SET state='stopped' WHERE id=3;`)
	if err != nil {
		t.Fatal(err)
	}
	err = st.DB.QueryRow(`SELECT NOT EXISTS (SELECT FROM delivery_outcome WHERE work_item_id=3)
        AND NOT EXISTS (SELECT FROM arm_delivery_results WHERE arm='candidate')`).Scan(&ok)
	if err != nil || !ok {
		t.Fatalf("stopped item without a PR entered the cohort: %v, %v", ok, err)
	}

	// A stop while the PR is held records the open hold up to the last
	// poll, and a hold that closes later updates the outcome.
	_, err = st.DB.Exec(`
        INSERT INTO pull_request(work_item_id,number,node_id,head_sha,state,held_since,polled_at)
        VALUES(4,5,'pr5','head','open',now()-interval '3 hours',now()-interval '1 hour');
        UPDATE work_item SET state='stopped' WHERE id=4;`)
	if err != nil {
		t.Fatal(err)
	}
	err = st.DB.QueryRow(`SELECT time_held = interval '2 hours' AND abandoned
        AND (SELECT n=1 AND merge_rate=0 AND tokens_per_merge IS NULL AND median_coder_minutes_per_merge IS NULL
             AND revert_rate IS NULL FROM arm_delivery_results
             WHERE arm='candidate' AND task_class='feature' AND guidance_version='guide2')
        FROM delivery_outcome WHERE node_id='pr5'`).Scan(&ok)
	if err != nil || !ok {
		t.Fatalf("open hold at stop: %v, %v", ok, err)
	}
	_, err = st.DB.Exec(`UPDATE pull_request SET polled_at=held_since+interval '150 minutes',
        held_duration=held_duration+interval '150 minutes', held_since=NULL WHERE node_id='pr5'`)
	if err != nil {
		t.Fatal(err)
	}
	err = st.DB.QueryRow(`SELECT time_held = interval '150 minutes' FROM delivery_outcome WHERE node_id='pr5'`).Scan(&ok)
	if err != nil || !ok {
		t.Fatalf("closed hold after stop: %v, %v", ok, err)
	}
}

//go:embed delivery_timings.sql
var deliveryTimings string

func TestDeliveryTimings(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
		INSERT INTO repo_binding (name,owner,repo,base_branch,itsaplan_project,kagent_namespace,kagent_agent,branch_prefix,max_engineering_retries)
		VALUES ('h','org','repo','main','HOME','factory','coder','factory/',2);
		INSERT INTO work_item (id,binding_name,issue_id,issue_key,branch,agent_key,kagent_namespace,kagent_agent,state,review_rounds,created_at)
		VALUES (1,'h',1,'H-1','b','coder','factory','coder','done',1,now()-interval '1 day'),
		       (2,'h',2,'H-2','c','coder','factory','coder','active',0,now()-interval '2 days');
		INSERT INTO run (id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,origin,issue_key,created_at)
		SELECT t.id, CASE WHEN t.id=1 THEN w.id END,'agent',t.trigger,'','',1,'closed',t.origin,w.issue_key,w.created_at+t.delay
		FROM work_item w CROSS JOIN (VALUES (1,'itsaplan','issue_delegated',interval '1 minute'),
		(-1,'reviewer','reviewer',interval '5 minutes'),
		(-2,'reviewer','reviewer',interval '8 minutes')) t(id,origin,trigger,delay) WHERE w.id=1;
		INSERT INTO delivery_attempt (run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,kagent_namespace,kagent_agent,state,writer_epoch,first_sent_at)
		SELECT id,1,1,1,'d','initial','m','factory','coder','completed',1,created_at+interval '1 minute' FROM run WHERE id=1;
		INSERT INTO pull_request (work_item_id,number,node_id,head_sha,state,created_at,merged_at)
		SELECT id,1,'node','sha','merged',created_at+interval '3 minutes',created_at+interval '10 minutes' FROM work_item WHERE id=1;
	`)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := st.DB.Query(deliveryTimings)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	want := map[string]float64{"queue wait": 60, "time to first PR": 180, "time to first recorded review dispatch attempt": 120, "rework rounds": 0.5, "time to merge": 600}
	for rows.Next() {
		var start, end any
		var metric, unit string
		var eligible, measured int
		var mean float64
		if err := rows.Scan(&start, &end, &metric, &unit, &eligible, &measured, &mean); err != nil {
			t.Fatal(err)
		}
		count := 1
		if metric == "rework rounds" {
			count = 2
		}
		expected, ok := want[metric]
		if !ok || eligible != 2 || measured != count || mean != expected {
			t.Errorf("%s: eligible=%d measured=%d mean=%v", metric, eligible, measured, mean)
		}
		delete(want, metric)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(want) > 0 {
		t.Fatal("missing metrics", want)
	}
}
