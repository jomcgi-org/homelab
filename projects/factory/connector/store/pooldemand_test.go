package store_test

import (
	"context"
	"reflect"
	"sort"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestPendingAttemptsCountsOnlyLiveAttemptsWithoutATask(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
	INSERT INTO repo_binding(name,owner,repo,base_branch,itsaplan_project,kagent_namespace,kagent_agent,branch_prefix,max_engineering_retries)
	VALUES ('b','org','repo','main','HOME','ns','coder','factory/',1);
	INSERT INTO work_item(id,binding_name,issue_id,issue_key,branch,agent_key,kagent_namespace,kagent_agent)
	SELECT i,'b',i,'HOME-'||i,'branch'||i,'coder','ns','coder' FROM generate_series(1,8) i;
	INSERT INTO run(id,work_item_id,agent_key,trigger,prompt,system_prompt,attempt,state,arm,task_class,base_sha)
	SELECT i,i,'coder','issue_delegated','','',1,'dispatched','control','task','base' FROM generate_series(1,8) i;
	-- An orphan: run 7 closed without finishing its attempt (failRun on a
	-- binding that is no longer configured). Run 8 carries a stop.
	UPDATE run SET state = 'closed' WHERE id = 7;
	UPDATE run SET stopped_reason = 'stopped' WHERE id = 8;
	INSERT INTO delivery_attempt(run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,
		kagent_namespace,kagent_agent,state,writer_epoch)
	VALUES (1,1,1,1,'d1','initial','m1','ns','coder','sending',1),
	       (2,1,1,1,'d2','initial','m2','ns','coder','ambiguous',1),
	       (3,1,1,1,'d3','initial','m3','ns','coder','accepted',1),
	       (4,1,1,1,'d4','initial','m4','ns','coder','completed',1),
	       (5,1,1,1,'d5','initial','m5','ns','coder','failed',1),
	       (6,1,1,1,'d6','initial','m6','other','reviewer','sending',1),
	       (7,1,1,1,'d7','initial','m7','ns','coder','sending',1),
	       (8,1,1,1,'d8','initial','m8','ns','coder','ambiguous',1)`)
	if err != nil {
		t.Fatal(err)
	}
	got, err := st.PendingAttempts(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	sort.Slice(got, func(i, j int) bool { return got[i].Namespace < got[j].Namespace })
	want := []store.AgentAttempts{
		{Namespace: "ns", Agent: "coder", Attempts: 2},
		{Namespace: "other", Agent: "reviewer", Attempts: 1},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("pending attempts = %+v, want %+v", got, want)
	}
}
