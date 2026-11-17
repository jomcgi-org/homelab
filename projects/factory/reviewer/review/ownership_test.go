package review

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestCancellationMarkerKeepsLegacyTaskIdentity(t *testing.T) {
	// The pre-journal reader on main (engine.go:RecordedTask).
	legacy := regexp.MustCompile(`<!-- factory-review:task attempt=([0-9]+) id=([^\s>]*) sent=([0-9]*)(?: issues=([a-f0-9]{64}))?(?: semgrep=([a-f0-9]{64}))? -->`)
	h := newHarness(t)
	for _, reason := range []string{"timeout", "superseded", "closed", "duplicate", "issues", "issues_pending", "semgrep"} {
		rec := recorded{Attempt: 2, ID: "original-task", Sent: time.Unix(1234, 0), Cancel: reason, Issues: strings.Repeat("a", 64), Semgrep: strings.Repeat("b", 64)}
		summary := progressSummary(&h.cfg.Bindings[0], &Gate{}, rec, "")
		m := legacy.FindStringSubmatch(summary)
		if len(m) != 6 || m[1] != "2" || m[2] != rec.ID || m[3] != "1234" || m[4] != rec.Issues || m[5] != rec.Semgrep {
			t.Fatalf("legacy reader lost %s cancellation's original task: %q", reason, summary)
		}
		if got := RecordedTask(summary); got != rec {
			t.Fatalf("new reader lost cancellation: %+v, want %+v", got, rec)
		}
	}
	// A cancellation from another attempt or task cannot stop this one.
	summary := "<!-- factory-review:task attempt=2 id=current sent=1234 -->\n" +
		"<!-- factory-review:cancel attempt=1 id=current reason=closed -->\n" +
		"<!-- factory-review:cancel attempt=2 id=other reason=closed -->"
	if rec := RecordedTask(summary); rec.ID != "current" || rec.Cancel != "" {
		t.Fatalf("borrowed another task's cancellation: %+v", rec)
	}
	// Accept the inline format as well when recovering an existing v2 journal.
	if rec := RecordedTask("<!-- factory-review:task attempt=2 id=original-task sent=1234 cancel=closed -->"); rec.Cancel != "closed" {
		t.Fatalf("lost older journal format: %+v", rec)
	}
}

func TestReaderReleaseDefersCreationButKeepsAnActivatedJournalOnRollback(t *testing.T) {
	h := newHarness(t)
	journal := filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.StatePath = journal
	h.cfg.DeferStateCreation = true
	h.cfg.Timeout = 50 * time.Millisecond
	addPR7(h)
	h.k.cancellation("fail", nil)
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	pendingCancellation(t, h, old)
	assertOwned(t, h, e, ctx, old)
	stop()
	if _, err := os.Stat(journal); !os.IsNotExist(err) {
		t.Fatalf("reader release created journal before activation: %v", err)
	}

	// Only the later release creates a journal. It adopts the same task from
	// the check, including the cancellation that the reader release started.
	h.cfg.DeferStateCreation = false
	e, ctx, stop = h.start()
	assertOwned(t, h, e, ctx, old)
	stop()
	read := func() []ownership {
		t.Helper()
		data, err := os.ReadFile(journal)
		if err != nil {
			t.Fatal(err)
		}
		var saved struct {
			Version int
			Reviews []ownership
		}
		if err := json.Unmarshal(data, &saved); err != nil || saved.Version != 2 {
			t.Fatalf("invalid activated journal: %s (%v)", data, err)
		}
		return saved.Reviews
	}
	if saved := read(); len(saved) != 1 || RecordedTask(saved[0].Run.Summary).ID != old {
		t.Fatalf("activation lost ownership: %+v", saved)
	}

	// Lowering the pin restores the reader's default, which must still load
	// and update the existing journal instead of disabling its ownership.
	h.cfg.DeferStateCreation = true
	e, ctx, stop = h.start()
	assertOwned(t, h, e, ctx, old)
	h.k.cancellation("", nil)
	eventually(t, "replacement after rollback reconciles cancellation", func() bool {
		e.Tick(ctx)
		return h.taskOn(7, shaA, 2) != ""
	})
	h.k.finish(h.taskOn(7, shaA, 2), kagent.StateCompleted, report(t, passing))
	eventually(t, "report after rollback", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
	stop()
	if saved := read(); len(saved) != 0 {
		t.Fatalf("rollback reader did not persist ownership release: %+v", saved)
	}
}

func pendingCancellation(t *testing.T, h *harness, task string) {
	t.Helper()
	eventually(t, "visible pending cancellation", func() bool {
		c := h.gh.check(shaA)
		return c != nil && c.Title == "Cancellation pending" && c.Status == "in_progress" && RecordedTask(c.Summary).ID == task
	})
}

func assertOwned(t *testing.T, h *harness, e *Engine, ctx context.Context, task string) {
	t.Helper()
	for range 5 {
		e.Tick(ctx)
		time.Sleep(5 * time.Millisecond)
	}
	if executions, _ := h.k.state(); executions != 1 {
		t.Fatalf("started %d executions while the old task was pending", executions)
	}
	if got := testutil.ToFloat64(h.m.InFlight); got != 1 {
		t.Fatalf("pending cancellation owns %v slots, want 1", got)
	}
	c := h.gh.check(shaA)
	if c.Status != "in_progress" || RecordedTask(c.Summary).ID != task || RecordedTask(c.Summary).Cancel == "" {
		t.Fatalf("lost cancellation ownership: %+v", c)
	}
}

func TestTimeoutCancellationRetainsTaskUntilTerminal(t *testing.T) {
	for _, mode := range []string{"fail", "delayed", "wrong", "nil"} {
		t.Run(mode, func(t *testing.T) {
			h := newHarness(t)
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			h.cfg.Timeout = 50 * time.Millisecond
			addPR7(h)
			h.k.cancellation(mode, nil)
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			pendingCancellation(t, h, old)
			assertOwned(t, h, e, ctx, old)
			stop()
			h.cfg.DeferStateCreation = true // The rollback release still owns existing journal entries.

			// A fresh process must retain the same task and cancellation
			// intent even though its attempt timeout has passed many times.
			h.cfg.Timeout = time.Hour
			e, ctx, _ = h.start()
			e.Tick(ctx)
			assertOwned(t, h, e, ctx, old)
			h.k.cancellation("", nil)
			eventually(t, "a retry after confirmed cancellation", func() bool {
				e.Tick(ctx)
				return h.taskOn(7, shaA, 2) != ""
			})
			oldTask, err := h.k.GetTask(ctx, testAgent, old)
			if err != nil || !kagent.Terminal(oldTask.Status.State) {
				t.Fatalf("replacement preceded quiescence: %v, %v", oldTask, err)
			}
			h.k.finish(h.taskOn(7, shaA, 2), kagent.StateCompleted, report(t, passing))
			eventually(t, "the replacement report", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
		})
	}
}

func TestSupersededAndClosedCancellationSurvivesRestart(t *testing.T) {
	for _, closed := range []bool{false, true} {
		t.Run(fmt.Sprintf("closed=%t", closed), func(t *testing.T) {
			h := newHarness(t)
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			// A free slot must not admit this PR's replacement prematurely.
			h.cfg.Concurrency = 2
			addPR7(h)
			h.k.cancellation("fail", nil)
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
			if closed {
				h.gh.mu.Lock()
				h.gh.prs[7].Closed = true
				h.gh.mu.Unlock()
			} else {
				h.gh.push(7, strings.Repeat("b", 40))
			}
			e.Tick(ctx)
			e.Tick(ctx)
			pendingCancellation(t, h, old)
			assertOwned(t, h, e, ctx, old)
			stop()
			h.cfg.DeferStateCreation = true // The rollback release still owns existing journal entries.

			e, ctx, _ = h.start()
			e.Tick(ctx)
			assertOwned(t, h, e, ctx, old)
			h.k.cancellation("", nil)
			eventually(t, "cancelled old check after transport recovers", func() bool {
				e.Tick(ctx)
				return h.gh.check(shaA).Conclusion == ConclusionCancelled
			})
			if !closed {
				eventually(t, "replacement head", func() bool {
					e.Tick(ctx)
					return h.taskOn(7, strings.Repeat("b", 40), 1) != ""
				})
			} else {
				eventually(t, "released capacity", func() bool { return testutil.ToFloat64(h.m.InFlight) == 0 })
			}
		})
	}
}

func TestLostCancellationResponseIsReconciledAfterRestart(t *testing.T) {
	h := newHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.Timeout = 50 * time.Millisecond
	h.cfg.TaskPoll = time.Hour // Stop before the next confirmation read.
	addPR7(h)
	h.k.cancellation("lost", nil)
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	// Expire immediately on reattachment instead of waiting TaskPoll.
	stop()
	h.cfg.DeferStateCreation = true // The rollback release still owns existing journal entries.
	h.cfg.Timeout = time.Nanosecond
	e, ctx, stop = h.start()
	e.Tick(ctx)
	eventually(t, "lost cancellation response", func() bool {
		_, cancels := h.k.state()
		return len(cancels) == 1
	})
	pendingCancellation(t, h, old)
	stop()
	h.cfg.TaskPoll = 2 * time.Millisecond
	h.cfg.Timeout = time.Hour
	e, ctx, _ = h.start()
	e.Tick(ctx)
	eventually(t, "retry after recovering terminal task", func() bool { return h.taskOn(7, shaA, 2) != "" })
	if _, cancels := h.k.state(); len(cancels) != 1 {
		t.Fatalf("retried cancellation of an already terminal task: %v", cancels)
	}
}

func TestRestartFindsWorkThatClosedWhileServiceWasDown(t *testing.T) {
	h := newHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	addPR7(h)
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
	stop()
	h.gh.mu.Lock()
	h.gh.prs[7].Closed = true
	h.gh.mu.Unlock()
	h.k.cancellation("fail", nil)
	e, ctx, _ = h.start()
	e.Tick(ctx)
	pendingCancellation(t, h, old)
	assertOwned(t, h, e, ctx, old)
	h.k.cancellation("", nil)
	eventually(t, "closed task reconciled", func() bool {
		e.Tick(ctx)
		return h.gh.check(shaA).Conclusion == ConclusionCancelled
	})
}

func TestRestartRefreshesHumanGateLabelsBeforeCompleting(t *testing.T) {
	for _, label := range []string{"human", "needs-human"} {
		for _, finished := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/finished=%t", label, finished), func(t *testing.T) {
				h := newHarness(t)
				h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
				addPR7(h)
				e, ctx, stop := h.start()
				task := runReview(t, h, e, ctx, 7, shaA)
				eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == task })
				stop()
				h.gh.label(7, label)
				if finished {
					h.k.finish(task, kagent.StateCompleted, report(t, passing))
				}
				e, ctx, _ = h.start()
				e.Tick(ctx)
				if !finished {
					h.k.finish(task, kagent.StateCompleted, report(t, passing))
				}
				// Do not tick again: normal polling must not hide a successful
				// completion from stale recovery inputs by fixing it later.
				eventually(t, "recovered review completes", func() bool { return h.gh.check(shaA).Status == "completed" })
				e.Wait()
				c := h.gh.check(shaA)
				st, _ := LoadState(c.Text)
				if c.Conclusion != ConclusionFailure || st.Gate == nil || strings.Join(st.Gate.Labels, ",") != label {
					t.Fatalf("recovered review ignored the current label: check=%+v, gate=%+v", c, st.Gate)
				}
				if got := testutil.ToFloat64(h.m.ReviewsCompleted.WithLabelValues("homelab", ConclusionSuccess)); got != 0 {
					t.Fatalf("published %v successful checks before applying the gate", got)
				}
				if executions, _ := h.k.state(); executions != 1 {
					t.Fatalf("recovery started %d executions, want the original task only", executions)
				}
			})
		}
	}
}

func TestRestartRetainsOwnershipWhenCurrentPRReadFails(t *testing.T) {
	h := newHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.Concurrency = 1
	addPR7(h)
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == task })
	stop()
	h.gh.label(7, "needs-human")
	h.gh.addPR(&fakePR{Number: 8, Head: strings.Repeat("b", 40)})
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	path := "GET /repos/" + testRepo + "/pulls/7"
	h.gh.failWith(path, 503)
	e, ctx, _ = h.start()
	for range 3 {
		e.Tick(ctx)
		e.mu.Lock()
		running := len(e.inflight)
		e.mu.Unlock()
		if running != 0 {
			t.Fatal("resumed a review without current PR metadata")
		}
		c := h.gh.check(shaA)
		if c.Status != "in_progress" || RecordedTask(c.Summary).ID != task {
			t.Fatalf("completed without current PR metadata: %+v", c)
		}
		if got := testutil.ToFloat64(h.m.InFlight); got != 1 {
			t.Fatalf("failed recovery retained %v slots, want 1", got)
		}
		if executions, _ := h.k.state(); executions != 1 || h.gh.checkCount() != 1 {
			t.Fatal("admitted another review while recovery still owned the only slot")
		}
	}
	h.gh.failWith(path, 0)
	e.Tick(ctx)
	eventually(t, "recovery respects the gate after reads recover", func() bool { return h.gh.check(shaA).Conclusion == ConclusionFailure })
}

func TestContextHoldRetainsCancellationUntilTerminal(t *testing.T) {
	for _, foreign := range []bool{false, true} {
		t.Run(fmt.Sprintf("foreign=%t", foreign), func(t *testing.T) {
			h := newHarness(t)
			journal := filepath.Join(t.TempDir(), "ownership.json")
			if !foreign {
				h.cfg.StatePath = journal
			}
			addPR7(h)
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
			h.k.cancellation("fail", nil)
			h.gh.addPR(&fakePR{Number: 8, Head: shaA})
			remaining := 7
			if foreign {
				// A check discovered without a journal, owned by the PR that
				// closed, must be adopted before its replacement can review.
				stop()
				h.gh.withLock(func() { h.gh.prs[7].Closed = true })
				h.cfg.StatePath = journal
				e, ctx, stop = h.start()
				remaining = 8
			}
			e.Tick(ctx)
			pendingCancellation(t, h, old)
			assertOwned(t, h, e, ctx, old)
			stop()
			if !foreign {
				h.gh.withLock(func() { h.gh.prs[8].Closed = true })
			}
			e, ctx, _ = h.start()
			e.Tick(ctx)
			assertOwned(t, h, e, ctx, old)
			h.k.cancellation("", nil)
			var replacement string
			eventually(t, "context review after confirmed cancellation", func() bool {
				e.Tick(ctx)
				replacement = h.taskOn(remaining, shaA, 1)
				return replacement != "" && replacement != old
			})
			oldTask, err := h.k.GetTask(ctx, testAgent, old)
			if err != nil || !kagent.Terminal(oldTask.Status.State) {
				t.Fatalf("context replacement preceded quiescence: %v, %v", oldTask, err)
			}
			h.k.finish(replacement, kagent.StateCompleted, report(t, passing))
			eventually(t, "replacement still requires human approval", func() bool { return h.gh.check(shaA).Conclusion == ConclusionFailure })
			st, _ := LoadState(h.gh.check(shaA).Text)
			if st.Gate == nil || !st.Gate.SharedHead {
				t.Fatal("cancellation lost the context hold")
			}
		})
	}
}

func TestPendingDuplicateCancellationStillReconcilesOtherGates(t *testing.T) {
	h, e, ctx := approvedQueuedHead(t)
	shaB := strings.Repeat("b", 40)
	h.gh.addPR(&fakePR{Number: 8, Head: shaB, Updated: time.Now().Add(time.Hour)})
	old := runReview(t, h, e, ctx, 8, shaB)
	eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaB).Summary).ID == old })
	h.k.cancellation("fail", nil)
	h.gh.addPR(&fakePR{Number: 9, Head: shaB, Updated: time.Now().Add(time.Hour)})
	h.gh.label(7, "needs-human")
	e.Tick(ctx) // Duplicates sort before the unrelated queued PR.
	if c := h.gh.check(shaB); c.Title != "Cancellation pending" || RecordedTask(c.Summary).ID != old {
		t.Fatalf("lost duplicate cancellation: %+v", c)
	}
	if c := h.gh.check(shaA); c.Conclusion != ConclusionFailure {
		t.Fatalf("unrelated PR kept a stale pass: %+v", c)
	}
	if c := h.gh.check("mg-7"); c.Conclusion != ConclusionFailure {
		t.Fatalf("unrelated merge group kept a stale pass: %+v", c)
	}
	if got := testutil.ToFloat64(h.m.InFlight); got != 1 {
		t.Fatalf("pending cancellation owns %v slots, want 1", got)
	}
}

func TestJournalFailureRevokesMergeGroupPass(t *testing.T) {
	h, e, ctx := approvedQueuedHead(t)
	e.cfg.StatePath = filepath.Join(t.TempDir(), "missing", "ownership.json")
	e.stateErr = errors.New("journal unavailable")
	e.Tick(ctx)
	if c := h.gh.check("mg-7"); c.Conclusion != ConclusionFailure {
		t.Fatalf("ownership failure left the merge group passing: %+v", c)
	}
}

func TestAcceptedSendSurvivesCheckpointFailureAndRestart(t *testing.T) {
	for _, closed := range []bool{false, true} {
		t.Run(fmt.Sprintf("closed=%t", closed), func(t *testing.T) {
			h := newHarness(t)
			root := t.TempDir()
			dir, saved := filepath.Join(root, "journal"), filepath.Join(root, "saved")
			if err := os.Mkdir(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			h.cfg.StatePath = filepath.Join(dir, "ownership.json")
			addPR7(h)
			var once sync.Once
			h.k.afterSend = func() {
				once.Do(func() {
					// Preserve the last durable bytes and make all subsequent
					// journal writes fail after the runtime accepted the task.
					if err := os.Rename(dir, saved); err != nil {
						t.Error(err)
						return
					}
					if err := os.WriteFile(dir, []byte("not a directory"), 0o600); err != nil {
						t.Error(err)
					}
				})
			}
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "worker stopped after checkpoint failure", func() bool {
				e.mu.Lock()
				defer e.mu.Unlock()
				return len(e.inflight) == 0 && e.stateErr != nil
			})
			if RecordedTask(h.gh.check(shaA).Summary).ID != "" {
				t.Fatal("test unexpectedly published the task ID to GitHub")
			}
			stop()
			if err := os.Remove(dir); err != nil {
				t.Fatal(err)
			}
			if err := os.Rename(saved, dir); err != nil {
				t.Fatal(err)
			}
			h.gh.withLock(func() {
				h.gh.prs[7].Body = "Changed while the service was stopped."
				h.gh.prs[7].Closed = closed
			})
			if !closed {
				h.gh.push(7, strings.Repeat("b", 40))
			}
			h.k.cancellation("fail", nil)
			h.k.mu.Lock()
			h.k.sendErr = []error{kagent.ErrAmbiguous}
			h.k.mu.Unlock()
			e, ctx, _ = h.start()
			eventually(t, "original task recovered for cancellation", func() bool {
				e.Tick(ctx)
				c := h.gh.check(shaA)
				return c.Title == "Cancellation pending" && RecordedTask(c.Summary).ID == old
			})
			assertOwned(t, h, e, ctx, old)
			if h.k.rejections() != 0 {
				t.Fatal("recovery rebuilt the original message with changed inputs")
			}
			h.k.cancellation("", nil)
			eventually(t, "original task stopped", func() bool {
				e.Tick(ctx)
				return h.gh.check(shaA).Conclusion == ConclusionCancelled
			})
			oldTask, err := h.k.GetTask(ctx, testAgent, old)
			if err != nil || !kagent.Terminal(oldTask.Status.State) {
				t.Fatalf("released an accepted task before quiescence: %v, %v", oldTask, err)
			}
		})
	}
}

func TestAmbiguousSendDoesNotReplayIntoALaterRejection(t *testing.T) {
	h := newHarness(t)
	h.cfg.MaxSends = 1
	addPR7(h)
	h.k.loseResponse = true
	var once sync.Once
	h.k.afterSend = func() {
		once.Do(func() {
			h.k.mu.Lock()
			h.k.sendErr = []error{kagent.ErrNotAccepted, kagent.ErrNotAccepted, kagent.ErrNotAccepted}
			h.k.mu.Unlock()
		})
	}
	e, ctx, _ := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "the original send resolved", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
	if len(h.k.sent()) != 1 {
		t.Fatal("replayed an uncertain send")
	}
	if executions, _ := h.k.state(); executions != 1 {
		t.Fatalf("created %d executions while the first send was unresolved", executions)
	}
	for _, m := range h.k.sent() {
		if m.MessageID != h.msgID(7, shaA, 1) {
			t.Fatalf("advanced to another message ID: %s", m.MessageID)
		}
	}
	h.k.finish(old, kagent.StateCompleted, report(t, passing))
	eventually(t, "original review completed", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
}

func TestMissingTaskDoesNotReleaseCancellationOwnership(t *testing.T) {
	h := newHarness(t)
	h.cfg.Timeout = 50 * time.Millisecond
	addPR7(h)
	e, ctx, _ := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	h.k.cancellation("fail", kagent.ErrTaskNotFound)
	pendingCancellation(t, h, old)
	assertOwned(t, h, e, ctx, old)
	h.k.cancellation("", nil)
}

func TestCancelResponseConfirmsTerminationWhenReadsFail(t *testing.T) {
	h := newHarness(t)
	h.cfg.Timeout = 50 * time.Millisecond
	addPR7(h)
	e, ctx, _ := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	h.k.cancellation("terminal", errors.New("task reads unavailable"))
	eventually(t, "retry after terminal cancellation response", func() bool { return h.taskOn(7, shaA, 2) != "" })
	h.k.mu.Lock()
	state := h.k.tasks[old].task.Status.State
	h.k.mu.Unlock()
	if !kagent.Terminal(state) {
		t.Fatalf("replacement started before cancellation: %s", state)
	}
}

func TestRecoveredCancellationKeepsCapacityAndOriginalAgent(t *testing.T) {
	h := newHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.Concurrency = 1
	addPR7(h)
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
	stop()
	h.cfg.Bindings[0].Agent.Name = "new-reviewer"
	h.gh.addPR(&fakePR{Number: 8, Head: strings.Repeat("b", 40)})
	h.k.cancellation("fail", nil)
	e, ctx, _ = h.start()
	e.Tick(ctx)
	pendingCancellation(t, h, old)
	assertOwned(t, h, e, ctx, old)
	h.k.cancellation("", nil)
	eventually(t, "cancellation on the original agent", func() bool {
		e.Tick(ctx)
		oldTask, err := h.k.GetTask(ctx, testAgent, old)
		return err == nil && kagent.Terminal(oldTask.Status.State)
	})
}

func TestCancellationWaitsForDurableVisibleIntent(t *testing.T) {
	h := newHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.Timeout = 50 * time.Millisecond
	addPR7(h)
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
	path := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
	h.gh.failWith(path, 503)
	eventually(t, "durable cancellation despite GitHub failure", func() bool {
		e.mu.Lock()
		defer e.mu.Unlock()
		return RecordedTask(e.owned[headKey{"homelab", shaA}].Run.Summary).Cancel == "timeout"
	})
	if _, cancels := h.k.state(); len(cancels) != 0 {
		t.Fatal("cancelled before publishing intent")
	}
	stop()
	h.k.cancellation("fail", nil)
	h.gh.failWith(path, 0)
	e, ctx, _ = h.start()
	e.Tick(ctx)
	pendingCancellation(t, h, old)
	assertOwned(t, h, e, ctx, old)
}

func TestOwnershipJournalFailureFencesNewWork(t *testing.T) {
	for _, contents := range []string{"", `{"Version":3}`, `{"Version":1,"Reviews":[{}]}`} {
		t.Run(fmt.Sprintf("journal=%q", contents), func(t *testing.T) {
			h := newHarness(t)
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			if err := os.WriteFile(h.cfg.StatePath, []byte(contents), 0o600); err != nil {
				t.Fatal(err)
			}
			addPR7(h)
			e := h.engine()
			e.Tick(context.Background())
			if e.loadErr == nil || h.gh.checkCount() != 0 || len(h.k.sent()) != 0 {
				t.Fatal("admitted work with unreadable ownership")
			}
		})
	}
}

// Metadata failures stop the worker but retain its execution slot until
// the check can be completed. Waiting for the ownership gauge to clear
// would confuse these two states and miss accidental ownership loss.
func waitForOwnedWorker(t *testing.T, h *harness, e *Engine, why string) {
	t.Helper()
	eventually(t, why, func() bool {
		e.mu.Lock()
		defer e.mu.Unlock()
		return len(e.inflight) == 0
	})
	if got := testutil.ToFloat64(h.m.InFlight); got != 1 {
		t.Fatalf("metadata failure retained %v execution slots, want 1", got)
	}
}

func TestRunWithUnreadableJournalRevokesExistingMergeGroups(t *testing.T) {
	for _, unreadable := range []bool{false, true} {
		t.Run(fmt.Sprintf("unreadable=%t", unreadable), func(t *testing.T) {
			h, previous, _ := approvedQueuedHead(t)
			previous.Wait()
			h.cfg.DeferStateCreation = true
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			if unreadable {
				if err := os.Mkdir(h.cfg.StatePath, 0o700); err != nil {
					t.Fatal(err)
				}
			} else if err := os.WriteFile(h.cfg.StatePath, []byte("invalid journal"), 0o600); err != nil {
				t.Fatal(err)
			}
			h.gh.label(7, "human")
			h.gh.addPR(&fakePR{Number: 8, Head: strings.Repeat("b", 40)})
			before := len(h.k.sent())
			e := h.engine()
			if e.loadErr == nil {
				t.Fatal("expected a journal load error")
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { done <- e.Run(ctx, time.Millisecond) }()
			eventually(t, "merge group revoked by the fenced process", func() bool {
				return h.gh.check("mg-7").Conclusion == ConclusionFailure
			})
			select {
			case err := <-done:
				t.Fatalf("fenced polling stopped: %v", err)
			default:
			}
			cancel()
			if err := <-done; !errors.Is(err, context.Canceled) {
				t.Fatalf("shutdown: %v", err)
			}
			if len(h.k.sent()) != before || h.gh.check(strings.Repeat("b", 40)) != nil {
				t.Fatal("the fenced process admitted a review")
			}
		})
	}
}

func TestUncertainSendKeepsOwnershipAfterReceiptDisappears(t *testing.T) {
	for _, state := range []string{"open", "closed", "superseded"} {
		t.Run(state, func(t *testing.T) {
			h := newPR7Harness(t)
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			h.cfg.Concurrency = 1
			h.cfg.Timeout = 20 * time.Millisecond
			h.k.loseResponse = true
			h.k.listErr = errors.New("listing unavailable")
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "visible unresolved dispatch", func() bool { return h.gh.check(shaA).Title == "Dispatch outcome unknown" })
			h.k.mu.Lock()
			delete(h.k.initial, h.msgID(7, shaA, 1))
			h.k.hidden = map[string]bool{old: true}
			h.k.listErr = nil
			h.k.mu.Unlock()
			stop()
			h.cfg.DeferStateCreation = true // The rollback release still owns existing journal entries.
			switch state {
			case "closed":
				h.gh.withLock(func() { h.gh.prs[7].Closed = true })
			case "superseded":
				h.gh.push(7, strings.Repeat("b", 40))
			}
			h.gh.addPR(&fakePR{Number: 8, Head: strings.Repeat("c", 40)})
			e, ctx, _ = h.start()
			for range 10 {
				e.Tick(ctx)
				time.Sleep(5 * time.Millisecond)
			}
			if executions, cancels := h.k.state(); executions != 1 || len(h.k.sent()) != 1 || len(cancels) != 0 {
				t.Fatalf("hidden execution replaced: executions=%d sends=%d cancels=%v", executions, len(h.k.sent()), cancels)
			}
			if got := testutil.ToFloat64(h.m.InFlight); got != 1 {
				t.Fatalf("unknown send owns %v slots, want 1", got)
			}
			c := h.gh.check(shaA)
			if c.Status != "in_progress" || !strings.Contains(c.Summary, "unresolved") {
				t.Fatalf("unknown dispatch is not visible: %+v", c)
			}
			// Only positive evidence of the original task permits recovery.
			h.k.mu.Lock()
			delete(h.k.hidden, old)
			h.k.cancelMode = "fail"
			h.k.mu.Unlock()
			eventually(t, "original task adopted without replay", func() bool {
				e.Tick(ctx)
				return RecordedTask(h.gh.check(shaA).Summary).ID == old
			})
			pendingCancellation(t, h, old)
			if executions, _ := h.k.state(); executions != 1 || len(h.k.sent()) != 1 {
				t.Fatal("recovering the original task sent another request")
			}
		})
	}
}

func TestUncertainSendScanFindsOriginalAcrossPages(t *testing.T) {
	for _, state := range []string{kagent.StateWorking, kagent.StateCompleted, kagent.StateInputRequired} {
		t.Run(state, func(t *testing.T) {
			h := newHarness(t)
			for i := range 101 {
				id := fmt.Sprintf("a-%03d", i)
				h.k.tasks[id] = &fakeTask{agent: testAgent, task: kagent.Task{
					ID: id, ContextID: "ctx-" + id, Status: kagent.TaskStatus{State: kagent.StateCompleted},
				}}
			}
			msg := kagent.Message{MessageID: "original", Parts: []kagent.Part{{Text: "original review"}}}
			old, err := h.k.Send(context.Background(), testAgent, msg)
			if err != nil {
				t.Fatal(err)
			}
			h.k.finish(old.ID, state, "result")
			delete(h.k.initial, msg.MessageID) // The creation receipt no longer exists.
			found, err := h.engine().findSentTask(context.Background(), testAgent, msg)
			if err != nil || found == nil || found.ID != old.ID || found.Status.State != state {
				t.Fatalf("original task = %+v, error = %v", found, err)
			}
			if len(h.k.sent()) != 1 {
				t.Fatal("task reconciliation dispatched another execution")
			}
		})
	}
}

func TestUncertainContinuationRecoversAfterRestart(t *testing.T) {
	h := newPR7Harness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	e, ctx, stop := h.start()
	finishHead(t, h, e, ctx, shaA, passing, nil)
	// Session planning must be able to read the previous session; fail only
	// after it has selected and sent the continuation.
	h.k.mu.Lock()
	h.k.loseResponse = true
	h.k.afterSend = func() {
		h.k.mu.Lock()
		defer h.k.mu.Unlock()
		h.k.listErr = errors.New("listing unavailable")
	}
	h.k.mu.Unlock()
	h.gh.push(7, sessionShaB)
	old := runReview(t, h, e, ctx, 7, sessionShaB)
	eventually(t, "unresolved continuation", func() bool { return h.gh.check(sessionShaB).Title == "Dispatch outcome unknown" })
	if lastSend(h).ContextID == "" {
		t.Fatal("expected a continuation")
	}
	stop()
	h.cfg.DeferStateCreation = true // The rollback release still owns existing journal entries.
	h.k.mu.Lock()
	h.k.listErr, h.k.afterSend = nil, nil
	h.k.mu.Unlock()
	e, ctx, _ = h.start()
	if e.loadErr != nil {
		t.Fatal(e.loadErr)
	}
	e.Tick(ctx)
	eventually(t, "continuation recovered by its opening message", func() bool {
		return RecordedTask(h.gh.check(sessionShaB).Summary).ID == old
	})
	if len(h.k.sent()) != 2 {
		t.Fatalf("continuation was resent: %d sends", len(h.k.sent()))
	}
	h.k.finish(old, kagent.StateCompleted, report(t, passing))
	eventually(t, "recovered continuation completed", func() bool { return h.gh.check(sessionShaB).Conclusion == ConclusionSuccess })
}

// Evidence added after HOME-30 must use the same cancellation fence as a
// timeout. Reattaching to changed or unreadable evidence cannot drop a task.
func TestEvidenceCancellationRetainsOwnershipAcrossRestart(t *testing.T) {
	for _, change := range []string{"issue", "unreadable", "semgrep"} {
		t.Run(change, func(t *testing.T) {
			h, api := issueHarness(t)
			h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
			h.cfg.Semgrep = &Semgrep{AppID: scanApp, CheckName: scanName, Wait: time.Nanosecond}
			h.gh.addCheck(shaA, scanName, scanApp, "success")
			e, ctx, stop := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "recorded evidence", func() bool {
				rec := RecordedTask(h.gh.check(shaA).Summary)
				return rec.ID == old && rec.Issues != "" && rec.Semgrep != ""
			})
			stop()
			switch change {
			case "issue":
				api.issue.Description = "Changed acceptance criterion"
			case "unreadable":
				api.fail = "issue"
			case "semgrep":
				h.gh.addCheck(shaA, scanName, scanApp, "failure")
			}
			h.k.cancellation("fail", nil)
			e, ctx, stop = h.start()
			e.Tick(ctx)
			pendingCancellation(t, h, old)
			assertOwned(t, h, e, ctx, old)
			stop()

			h.cfg.DeferStateCreation = true
			e, ctx, _ = h.start()
			assertOwned(t, h, e, ctx, old)
			h.k.cancellation("terminal", nil)
			eventually(t, "evidence cancellation confirmed", func() bool {
				e.Tick(ctx)
				task, err := h.k.GetTask(ctx, testAgent, old)
				return err == nil && kagent.Terminal(task.Status.State)
			})
			if change == "unreadable" {
				eventually(t, "issue hold after cancellation", func() bool {
					return h.gh.check(shaA).Status == "completed"
				})
				e.Wait()
				if executions, _ := h.k.state(); executions != 1 {
					t.Fatal("unreadable evidence dispatched a replacement")
				}
			} else {
				eventually(t, "review current evidence", func() bool { return h.taskOn(7, shaA, 2) != "" })
			}
		})
	}
}

func TestUncertainSendKeepsItsEvidenceAcrossRestart(t *testing.T) {
	h, _ := issueHarness(t)
	h.cfg.StatePath = filepath.Join(t.TempDir(), "ownership.json")
	h.cfg.Semgrep = &Semgrep{AppID: scanApp, CheckName: scanName, Wait: time.Nanosecond}
	h.gh.addCheck(shaA, scanName, scanApp, "success")
	h.k.loseResponse = true
	e, ctx, stop := h.start()
	old := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "recovered send evidence", func() bool {
		rec := RecordedTask(h.gh.check(shaA).Summary)
		return rec.ID == old && rec.Issues != "" && rec.Semgrep != ""
	})
	stop()
	h.cfg.DeferStateCreation = true
	e, ctx, _ = h.start()
	e.Tick(ctx)
	h.k.finish(old, kagent.StateCompleted, report(t, passing))
	eventually(t, "original evidence reviewed", func() bool {
		return h.gh.check(shaA).Conclusion == ConclusionSuccess
	})
	codeSends := 0
	for _, msg := range h.k.sent() {
		if !strings.Contains(msg.MessageID, "/goal/") {
			codeSends++
		}
	}
	if codeSends != 1 {
		t.Fatalf("sent %d code reviews after an uncertain response", codeSends)
	}
}
