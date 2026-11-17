package dispatch_test

import (
	"fmt"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestMetadataReadsAreJournaledBeforeSendingAcrossRestarts(t *testing.T) {
	v, wire := newReconciliationWire(t, 2)
	v.dispatchReconciliationInitial()
	a := v.latest(1)
	messageID := fmt.Sprintf("task-metadata/%d", a.ID)
	var initialAttempts int
	if err := v.st.DB.QueryRow(`SELECT attempts FROM task_metadata_read WHERE message_id = $1`, messageID).Scan(&initialAttempts); err != nil {
		t.Fatal(err)
	}
	var calls int
	var lastEpoch int64
	wire.onMetadata = func(id, taskID string) {
		calls++
		var attempts int
		var epoch, attemptID int64
		var recordedTask, contextID string
		err := v.st.DB.QueryRow(`SELECT attempts, writer_epoch, attempt_id, task_id, context_id
			FROM task_metadata_read WHERE message_id = $1`, id).
			Scan(&attempts, &epoch, &attemptID, &recordedTask, &contextID)
		if err != nil {
			t.Errorf("metadata reached kagent without a durable attempt: %v", err)
			return
		}
		if id != messageID || attempts != initialAttempts+calls || attemptID != a.ID || recordedTask != a.TaskID || taskID != a.TaskID || contextID != a.ContextID || epoch < lastEpoch {
			t.Errorf("metadata journal: id=%q attempts=%d epoch=%d task=%q context=%q", id, attempts, epoch, recordedTask, contextID)
		}
		lastEpoch = epoch
	}
	wire.metadataUnavailable = true
	v.tick(v.e)
	firstEpoch := lastEpoch
	v.restartWire(t, wire)
	v.tick(v.e)
	if calls != 2 || lastEpoch <= firstEpoch {
		t.Fatalf("failed polls did not retry under the new writer: calls=%d epoch=%d old=%d", calls, lastEpoch, firstEpoch)
	}
	wire.metadataUnavailable = false
	if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
		t.Fatal(err)
	}
	v.tick(v.e)
	v.tick(v.e)
	if calls != 4 || len(v.pendingCancellations()) != 0 || v.latest(1).State != store.AttemptCanceled {
		t.Fatalf("cancellation did not reuse the journal and fetch fresh state: calls=%d attempt=%+v", calls, v.latest(1))
	}
}

func TestMetadataReadRequiresDurableAttemptAndWriter(t *testing.T) {
	for _, cancellation := range []bool{false, true} {
		for _, failure := range []string{"journal unavailable", "writer fenced"} {
			t.Run(fmt.Sprintf("cancel=%t/%s", cancellation, failure), func(t *testing.T) {
				v, wire := newReconciliationWire(t, 2)
				v.dispatchReconciliationInitial()
				wire.onMetadata = func(string, string) { t.Error("metadata read without journal or writer authority") }
				if cancellation {
					if _, err := v.st.StopIssue(v.ctx, "HL-1", "operator canceled", "operator:test"); err != nil {
						t.Fatal(err)
					}
				}
				if failure == "journal unavailable" {
					if _, err := v.st.DB.Exec(`ALTER TABLE task_metadata_read ADD CONSTRAINT reject_read CHECK (false) NOT VALID`); err != nil {
						t.Fatal(err)
					}
					v.tick(v.e)
				} else {
					if _, err := v.st.DB.Exec(`UPDATE writer SET epoch = epoch + 1`); err != nil {
						t.Fatal(err)
					}
					if err := v.e.Tick(v.ctx); err == nil {
						t.Fatal("fenced writer tick succeeded")
					}
				}
				if !v.latest(1).Open() || len(v.its.Run(1).results) != 0 {
					t.Fatal("unrecorded metadata released execution ownership")
				}
			})
		}
	}
}
