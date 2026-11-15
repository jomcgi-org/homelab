package review

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestPreexistingPRKeepsAutonomousPathAcrossRestarts(t *testing.T) {
	h := newPR7Harness(t)
	h.gh.withLock(func() { h.gh.prs[7].Created = time.Now().Add(-24*time.Hour) })
	_, _, stop := startPassedReview(t, h)
	stop()
	h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
	for range 2 {
		e, ctx, stop := h.start()
		e.Tick(ctx)
		failIf(t, h.gh.check(shaA).Conclusion != ConclusionSuccess, "restart imposed a hold")
		stop()
	}
	failIf(t, len(h.gh.enqueues()) != 1, "unheld PR did not stay enqueueable")
}

func TestReopenedAndLaterPRsNeedNoRestartApproval(t *testing.T) {
	h := newPR7Harness(t)
	head := strings.Repeat("b", 40)
	h.gh.addPR(&fakePR{Number: 8, Head: head, Closed: true, Created: time.Now().Add(-time.Hour)})
	_, _, stop := startPassedReview(t, h)
	stop()
	h.gh.withLock(func() { h.gh.prs[8].Closed = false })
	e, ctx, _ := h.start()
	completeReview(t, h, runReview(t, h, e, ctx, 8, head), head, "reopened PR review")
	failIf(t, h.gh.check(head).Conclusion != ConclusionSuccess, "reopened PR acquired a restart hold")
	e.Wait()
	head = strings.Repeat("c", 40)
	h.gh.addPR(&fakePR{Number: 9, Head: head})
	completeReview(t, h, runReview(t, h, e, ctx, 9, head), head, "new PR review")
	failIf(t, h.gh.check(head).Conclusion != ConclusionSuccess, "new PR acquired a restart hold")
}

func closedLegacyGate(t *testing.T, gate *Gate, approved bool) *harness {
	t.Helper()
	h := newPR7Harness(t)
	conclusion := ConclusionFailure
	if approved {
		gate.Approver = "jomcgi"
		conclusion = ConclusionSuccess
		h.gh.review(7, "jomcgi", "APPROVED", shaA, "before closing")
	}
	h.gh.addCheck(shaA, "factory review", testAppID, conclusion)
	h.gh.withLock(func() {
		h.gh.prs[7].Closed = true
		c := h.gh.checks[len(h.gh.checks)-1]
		c.ExternalID = MessageID(testRepo, 7, shaA)
		c.Text = encodeState(State{Gate: gate})
		c.Summary = gateOnlyMarker
	})
	e, ctx, stop := h.start()
	failIf(t, !e.load(ctx), "initial import failed")
	stop()
	h.gh.withLock(func() { h.gh.prs[7].Closed = false })
	return h
}

func TestReopenedLegacyPRRestoresAndRevalidatesItsGate(t *testing.T) {
	for _, kind := range []string{"path", "finding", "removed label"} {
		for _, approved := range []bool{false, true} {
			name := kind
			if approved {
				name += "/approved"
			}
			t.Run(name, func(t *testing.T) {
				gate := &Gate{}
				switch kind {
				case "path":
					gate.Paths = []string{"REVIEW.md"}
				case "finding":
					gate.Findings = []Finding{{ID: "app.go:human-gate", Severity: SeverityHigh, Title: "Human decision required"}}
				case "removed label":
					gate.Labels = []string{"human"}
					gate.ObservedLabels = []string{}
				}
				h := closedLegacyGate(t, gate, approved)
				e, ctx, _ := h.start()
				e.Tick(ctx)
				want := ConclusionFailure
				if approved {
					want = ConclusionSuccess
				}
				st, _ := LoadState(h.gh.check(shaA).Text)
				failIf(t, h.gh.check(shaA).Conclusion != want || !st.Gate.Needed(), "reopening erased the gate or its valid approval")
				if approved {
					h.gh.review(7, "jomcgi", "DISMISSED", shaA, "withdraw approval")
					e.Tick(ctx)
					failIf(t, h.gh.check(shaA).Conclusion != ConclusionFailure, "restored approval was not revalidated")
				} else {
					approveAndCheck(t, h, e, ctx, 7, shaA, "approve restored gate", "fresh approval did not clear the restored gate")
				}
			})
		}
	}
}

func TestReopenedGateRestorationRetriesCompleteReadsAcrossRestart(t *testing.T) {
	for _, read := range []string{"commits/" + shaA + "/check-runs", "issues/7/events"} {
		t.Run(read, func(t *testing.T) {
			h := closedLegacyGate(t, &Gate{Paths: []string{"REVIEW.md"}}, true)
			h.gh.review(7, "jomcgi", "DISMISSED", shaA, "withdrawn while closed")
			h.gh.addCheck("mg-7", "factory review", testAppID, ConclusionSuccess)
			h.gh.withLock(func() { h.gh.queue = []github.MergeQueueEntry{{HeadCommit: "mg-7", Number: 7, PRHeadSHA: shaA}} })
			call := "GET /repos/" + testRepo + "/" + read
			h.gh.failWith(call, 502)
			e, ctx, stop := h.start()
			e.Tick(ctx)
			data, err := h.writer.Load(ctx)
			if err != nil {
				t.Fatal(err)
			}
			var saved durableState
			if err := json.Unmarshal(data, &saved); err != nil {
				t.Fatal(err)
			}
			m := saved.Contexts[testRepo+"#7"]
			failIf(t, m == nil || m.Restored || h.gh.check("mg-7").Conclusion != ConclusionFailure || len(h.gh.enqueues()) != 0, "incomplete restoration admitted the reopened PR")
			stop()
			h.gh.failWith(call, 0)
			e, ctx, _ = h.start()
			e.Tick(ctx)
			st, _ := LoadState(h.gh.check(shaA).Text)
			failIf(t, h.gh.check(shaA).Conclusion != ConclusionFailure || st.Gate == nil || len(st.Gate.Paths) == 0, "restart lost the unrestored legacy gate")
		})
	}
}

func TestReturningToAnEarlierHeadRestoresItsRecordedGate(t *testing.T) {
	h := newPR7Harness(t)
	h.gh.addCheck(shaA, "factory review", testAppID, ConclusionFailure)
	h.gh.withLock(func() {
		c := h.gh.checks[len(h.gh.checks)-1]
		c.ExternalID = MessageID(testRepo, 7, shaA)
		c.Text = encodeState(State{Gate: &Gate{Paths: []string{"REVIEW.md"}}})
		c.Summary = gateOnlyMarker
	})
	h.gh.push(7, strings.Repeat("b", 40))
	e, ctx, _ := h.start()
	failIf(t, !e.load(ctx), "initial import failed")
	h.gh.push(7, shaA)
	e.Tick(ctx)
	st, _ := LoadState(h.gh.check(shaA).Text)
	failIf(t, h.gh.check(shaA).Conclusion != ConclusionFailure || st.Gate == nil || len(st.Gate.Paths) == 0, "returning to an earlier head erased its gate")
}

func TestRestorationReadFailureCannotMakeAnUnreviewedHeadPass(t *testing.T) {
	for _, conclusion := range []string{"", ConclusionCancelled, ConclusionFailure} {
		t.Run("prior/"+conclusion, func(t *testing.T) {
			h := newPR7Harness(t)
			h.gh.withLock(func() { h.gh.prs[7].Closed = true })
			e, ctx, _ := h.start()
			failIf(t, !e.load(ctx), "initial import failed")
			h.gh.withLock(func() { h.gh.prs[7].Closed = false })
			if conclusion != "" {
				h.gh.addCheck(shaA, "factory review", testAppID, conclusion)
				h.gh.withLock(func() {
					h.gh.checks[len(h.gh.checks)-1].ExternalID = MessageID(testRepo, 7, shaA)
				})
			}
			read := "GET /repos/" + testRepo + "/issues/7/events"
			h.gh.failWith(read, 502)
			before := h.gh.checkCount()
			e.Tick(ctx)
			failIf(t, h.gh.checkCount() != before, "restoration replaced an unreviewed check with a gate-only result")
			h.gh.failWith(read, 0)
			e.Tick(ctx)
			check := h.gh.check(shaA)
			failIf(t, check != nil && check.Conclusion == ConclusionSuccess, "restoration bypassed review")
		})
	}
}

func TestImportDropsRestartOnlyHold(t *testing.T) {
	h := newPR7Harness(t)
	h.gh.addCheck(shaA, "factory review", testAppID, ConclusionFailure)
	h.gh.withLock(func() {
		c := h.gh.checks[len(h.gh.checks)-1]
		c.ExternalID = MessageID(testRepo, 7, shaA)
		c.Summary = "Waiting for recovery\n" + gateOnlyMarker
		legacy := `{"blocking":[],"gate":{"recovery_epoch":"old","recovery_pr":7,"approval_nonce":"old","approval_snapshot":{"ids":[],"all":true}}}`
		c.Text = statePrefix + base64.StdEncoding.EncodeToString([]byte(legacy)) + stateSuffix
	})
	e, ctx, stop := h.start()
	e.Tick(ctx)
	failIf(t, h.gh.check(shaA).Conclusion != ConclusionSuccess, "import retained the retired recovery hold")
	stop()
	e, ctx, _ = h.start()
	e.Tick(ctx)
	failIf(t, h.gh.check(shaA).Conclusion != ConclusionSuccess, "restart resurrected imported recovery state")
}

func TestImportCommitsSharedHeadsBeforeFirstProjection(t *testing.T) {
	h := newPR7Harness(t)
	h.gh.addPR(&fakePR{Number: 8, Head: shaA})
	e, ctx, stop := h.start()
	failIf(t, !e.load(ctx), "import failed")
	failIf(t, h.gh.checkCount() != 0, "import unexpectedly wrote a GitHub check")
	stop()
	h.gh.withLock(func() { h.gh.prs[7].Closed = true })
	head := strings.Repeat("b", 40)
	h.gh.push(8, head)
	e, ctx, _ = h.start()
	completeReview(t, h, runReview(t, h, e, ctx, 8, head), head, "imported shared-head review")
	failIf(t, h.gh.check(head).Conclusion != ConclusionFailure, "import lost an observed duplicate before its first projection")
}

func TestImportIsAtomicAndRequiresCompleteHistory(t *testing.T) {
	for _, failure := range []string{"checks", "labels", "incomplete labels", "listing"} {
		t.Run(failure, func(t *testing.T) {
			h := newPR7Harness(t)
			h.gh.withLock(func() {
				switch failure {
				case "checks":
					h.gh.fail["GET /repos/"+testRepo+"/commits/"+shaA+"/check-runs"] = 502
				case "labels":
					h.gh.fail["GET /repos/"+testRepo+"/issues/7/events"] = 502
				case "incomplete labels":
					for i := range github.MaxIssueEventPages * github.MaxListed {
						h.gh.prs[7].LabelEvents = append(h.gh.prs[7].LabelEvents, github.LabelEvent{ID: int64(i+1), Label: "human", CreatedAt: time.Now()})
					}
				case "listing":
					h.gh.fail["GET /repos/"+testRepo+"/pulls"] = 502
				}
			})
			e, ctx, _ := h.start()
			e.Tick(ctx)
			data, err := h.writer.Load(ctx)
			if err != nil {
				t.Fatal(err)
			}
			failIf(t, data != nil || h.gh.checkCount() != 0 || len(h.gh.enqueues()) != 0, "failed import acted or committed a marker")
		})
	}
}

func TestImportMarkerSurvivesRestart(t *testing.T) {
	h := newPR7Harness(t)
	e, ctx, stop := h.start()
	failIf(t, !e.load(ctx), "initial import failed")
	stop()
	h.gh.failWith("GET /repos/"+testRepo+"/pulls", 502)
	h.gh.failWith("GET /repos/"+testRepo+"/issues/7/events", 502)
	e, ctx, _ = h.start()
	failIf(t, !e.load(ctx), "a completed import ran again after restart")
}

func TestIncompleteImportRevokesQueuedPassAcrossRestart(t *testing.T) {
	for _, failure := range []string{"checks", "labels", "incomplete labels", "listing", "group check"} {
		for _, lostWrite := range []bool{false, true} {
			name := failure
			if lostWrite {
				name += "/lost write"
			}
			t.Run(name, func(t *testing.T) {
				h := newPR7Harness(t)
				h.gh.addCheck(shaA, "factory review", testAppID, ConclusionSuccess)
				h.gh.addCheck("mg-7", "factory review", testAppID, ConclusionSuccess)
				h.gh.withLock(func() {
					h.gh.queue = []github.MergeQueueEntry{{HeadCommit: "mg-7", Number: 7, PRHeadSHA: shaA}}
					switch failure {
					case "checks":
						h.gh.fail["GET /repos/"+testRepo+"/commits/"+shaA+"/check-runs"] = 502
					case "labels":
						h.gh.fail["GET /repos/"+testRepo+"/issues/7/events"] = 502
					case "incomplete labels":
						for i := range github.MaxIssueEventPages * github.MaxListed {
							h.gh.prs[7].LabelEvents = append(h.gh.prs[7].LabelEvents, github.LabelEvent{ID: int64(i + 1), Label: "human", CreatedAt: time.Now()})
						}
					case "listing":
						h.gh.fail["GET /repos/"+testRepo+"/pulls"] = 502
					case "group check":
						h.gh.fail["GET /repos/"+testRepo+"/pulls"] = 502
						h.gh.fail["GET /repos/"+testRepo+"/commits/mg-7/check-runs"] = 502
					}
				})
				if lostWrite {
					h.gh.failCheckWrites("mg-7", 502)
				}
				e, ctx, stop := h.start()
				e.Tick(ctx)
				data, err := h.writer.Load(ctx)
				if err != nil {
					t.Fatal(err)
				}
				var saved durableState
				if err := json.Unmarshal(data, &saved); err != nil {
					t.Fatal(err)
				}
				failIf(t, saved.Imported || len(saved.Contexts) != 0, "a failure projection completed the import")
				if lostWrite {
					failIf(t, saved.Projections[testRepo+"@mg-7"].Write.Conclusion != ConclusionFailure, "lost revocation was not durable")
				} else {
					failIf(t, h.gh.check("mg-7").Conclusion != ConclusionFailure, "incomplete import preserved a queued pass")
				}
				stop()
				h.gh.failCheckWrites("mg-7", 0)
				e, ctx, _ = h.start()
				e.Tick(ctx)
				failIf(t, e.loaded || h.gh.check("mg-7").Conclusion != ConclusionFailure || len(h.gh.enqueues()) != 0, "restart skipped import or failed to revoke the queued pass")
				h.gh.withLock(func() {
					clear(h.gh.fail)
					if failure == "incomplete labels" {
						h.gh.prs[7].LabelEvents = nil
					}
				})
				failIf(t, !e.load(ctx), "import did not recover when complete reads became available")
			})
		}
	}
}

func TestDatabaseFailureProducesNoPassOrEnqueue(t *testing.T) {
	for _, stage := range []string{"completion", "enqueue", "merge group"} {
		t.Run(stage, func(t *testing.T) {
			h := newPR7Harness(t)
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			eventually(t, "recorded task", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == task })
			if stage != "completion" {
				completeReview(t, h, task, shaA, "initial review")
				e.Wait()
			}
			if _, err := h.db.DB.Exec(`ALTER TABLE reviewer_state ADD CONSTRAINT unavailable CHECK (false) NOT VALID`); err != nil {
				t.Fatal(err)
			}
			h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
			if stage == "merge group" {
				h.gh.withLock(func() { h.gh.queue = []github.MergeQueueEntry{{HeadCommit: "mg-7", Number: 7, PRHeadSHA: shaA}} })
			}
			before := h.gh.checkCount()
			if stage == "completion" {
				h.k.finish(task, kagent.StateCompleted, report(t, passing))
				eventually(t, "failed commit", func() bool { return e.storageError() != nil })
			}
			e.Tick(ctx)
			failIf(t, e.storageError() == nil || h.gh.checkCount() != before || len(h.gh.enqueues()) != 0, "database failure allowed a side effect")
			if stage == "completion" {
				failIf(t, h.gh.check(shaA).Conclusion == ConclusionSuccess, "failed commit passed a review")
			}
		})
	}
}

func TestLostWriterStopsEngineWrites(t *testing.T) {
	h := newPR7Harness(t)
	e, _, stop := startPassedReview(t, h)
	stop()
	h.writer.Release()
	before := h.gh.checkCount()
	h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
	err := e.Run(context.Background(), time.Millisecond)
	failIf(t, err == nil || h.gh.checkCount() != before || len(h.gh.enqueues()) != 0, "lost writer continued")
}

func TestLateHoldSurvivesLostProjectionAndRestart(t *testing.T) {
	h, e, ctx := approvedQueuedHead(t)
	h.gh.failCheckWrites(shaA, 502)
	h.gh.label(7, "human")
	e.Tick(ctx)
	h.gh.label(7)
	h.gh.failCheckWrites(shaA, 0)
	e = h.engine()
	e.Tick(ctx)
	st, _ := LoadState(h.gh.check(shaA).Text)
	failIf(t, h.gh.check(shaA).Conclusion != ConclusionFailure || st.Gate == nil || len(st.Gate.Labels) == 0 || len(h.gh.enqueues()) != 0, "lost projection erased the real hold")
	approveAndCheck(t, h, e, ctx, 7, shaA, "fresh approval", "fresh approval did not clear the persisted hold")
}

func TestLaterDurableHoldRejectsStaleSuccess(t *testing.T) {
	h := newPR7Harness(t)
	e, ctx, _ := startPassedReview(t, h)
	old := h.gh.check(shaA)
	st, _ := LoadState(old.Text)
	pr := github.PullRequest{Number: 7, HeadSHA: shaA}
	b := &e.cfg.Bindings[0]
	gate := &Gate{Labels: []string{"human"}}
	gate.resetApproval(time.Now())
	e.retainGate(ctx, b, pr, gate, true)
	_, err := e.createCheck(ctx, b, github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: shaA, ExternalID: MessageID(testRepo, 7, shaA),
		Status: "completed", Conclusion: ConclusionSuccess,
		Output: &github.CheckRunOutput{Text: encodeState(st)},
	})
	failIf(t, err == nil, "a stale worker passed after a newer hold committed")
}

// checkBarrierStore pauses publication after its intent commits, exactly
// where a later observation used to overtake the success sent to GitHub.
type checkBarrierStore struct {
	StateStore
	reached chan struct{}
	resume  chan struct{}
}

func (s checkBarrierStore) Check(ctx context.Context) error {
	close(s.reached)
	select {
	case <-s.resume:
		return s.StateStore.Check(ctx)
	case <-ctx.Done():
		return ctx.Err()
	}
}

func TestSuccessPublicationSerializesWithHoldObservation(t *testing.T) {
	h := newPR7Harness(t)
	e, parent, _ := startPassedReview(t, h)
	e.Wait()
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	st, _ := LoadState(h.gh.check(shaA).Text)
	b := &e.cfg.Bindings[0]
	barrier := checkBarrierStore{StateStore: h.writer, reached: make(chan struct{}), resume: make(chan struct{})}
	e.cfg.Store = barrier
	release := sync.OnceFunc(func() { close(barrier.resume) })
	var workers sync.WaitGroup
	defer func() {
		release()
		workers.Wait()
	}()
	posted := make(chan error, 1)
	workers.Add(1)
	go func() {
		defer workers.Done()
		_, err := e.createCheck(ctx, b, github.CheckRunWrite{
			Name: e.cfg.CheckName, HeadSHA: shaA, ExternalID: MessageID(testRepo, 7, shaA),
			Status: "completed", Conclusion: ConclusionSuccess,
			Output: &github.CheckRunOutput{Text: encodeState(st)},
		})
		posted <- err
	}()
	select {
	case <-barrier.reached:
	case <-ctx.Done():
		t.Fatal("publication did not reach its writer check")
	}
	observed := make(chan struct{})
	workers.Add(1)
	go func() {
		defer workers.Done()
		gate := &Gate{Labels: []string{"human"}}
		gate.resetApproval(time.Now())
		e.retainGate(ctx, b, github.PullRequest{Number: 7, HeadSHA: shaA}, gate, true)
		close(observed)
	}()
	select {
	case <-observed:
		t.Error("a hold committed while an older success was waiting to publish")
	case <-time.After(50 * time.Millisecond):
	}
	release()
	if err := <-posted; err != nil {
		t.Fatal(err)
	}
	<-observed
	e.cfg.Store = h.writer
	_, err := e.createCheck(ctx, b, github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: shaA, ExternalID: MessageID(testRepo, 7, shaA),
		Status: "completed", Conclusion: ConclusionSuccess,
		Output: &github.CheckRunOutput{Text: encodeState(st)},
	})
	failIf(t, err == nil, "publication after the hold reused the previous approval")
}

func TestDuplicateObservationCommitsEverySiblingBeforeProjection(t *testing.T) {
	h := newPR7Harness(t)
	e, ctx, stop := startPassedReview(t, h)
	h.gh.addPR(&fakePR{Number: 8, Head: shaA})
	prs := []github.PullRequest{{Number: 7, HeadSHA: shaA, BaseRef: "main"}, {Number: 8, HeadSHA: shaA, BaseRef: "main"}}
	b := &e.cfg.Bindings[0]
	counts, labels := readyHeads(b, prs)
	failIf(t, !e.observeListing(ctx, b, prs, true, counts, labels), "observation did not commit")
	stop() // Crash point: no hold has reached GitHub.
	h.gh.withLock(func() { h.gh.prs[7].Closed = true })
	head := strings.Repeat("b", 40)
	h.gh.push(8, head)
	e, ctx, _ = h.start()
	completeReview(t, h, runReview(t, h, e, ctx, 8, head), head, "surviving sibling review")
	failIf(t, h.gh.check(head).Conclusion != ConclusionFailure, "a sibling lost the shared-head observation")
}
