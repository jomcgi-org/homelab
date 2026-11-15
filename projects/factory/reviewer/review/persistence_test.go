package review

import (
	"context"
	"encoding/base64"
	"strings"
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
