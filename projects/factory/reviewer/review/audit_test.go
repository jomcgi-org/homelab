package review

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func auditRecords(t *testing.T, s *FileGoalAudit) []GoalAuditRecord {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(s.dir, "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	var records []GoalAuditRecord
	for _, file := range files {
		b, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var r GoalAuditRecord
		if err := json.Unmarshal(b, &r); err != nil {
			t.Fatal(err)
		}
		records = append(records, r)
		info, err := os.Stat(file)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatal("audit file must be private")
		}
	}
	return records
}

func TestGoalAuditSurvivesRestartWithoutExtendingRetention(t *testing.T) {
	dir := t.TempDir()
	s, err := OpenGoalAudit(dir)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	r := GoalAuditRecord{
		Repository: testRepo, PullRequest: 8122, Head: shaA, Issue: "HOME-120", Digest: "digest",
		MessageID: "../../message", TaskID: "task", SessionID: "session", Phase: "initial",
		CreatedAt: now, State: kagent.StateCompleted,
		Reply: strings.Repeat("PRIVATE\x00", maxReply/8+1) + "full reply tail",
	}
	if err := s.Save(r); err != nil {
		t.Fatal(err)
	}
	s, err = OpenGoalAudit(dir)
	if err != nil {
		t.Fatal(err)
	}
	retry := r
	retry.CreatedAt = now.Add(24 * time.Hour)
	if err := s.Save(retry); err != nil {
		t.Fatal(err)
	}
	if got := auditRecords(t, s); !reflect.DeepEqual(got, []GoalAuditRecord{r}) {
		t.Fatal("restart or retry changed the audit record, or clipped its reply")
	}
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatal("audit directory must be private")
	}
	if err := s.Prune(now.Add(goalAuditRetention - time.Second)); err != nil || len(auditRecords(t, s)) != 1 {
		t.Fatal("audit expired before 14 days", err)
	}
	newer := r
	newer.MessageID, newer.CreatedAt, newer.Reply = "newer", now.Add(time.Hour), "newer reply"
	if err := s.Save(newer); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".reply-interrupted"), []byte("partial"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := s.Prune(now.Add(goalAuditRetention)); err != nil {
		t.Fatal(err)
	}
	if got := auditRecords(t, s); !reflect.DeepEqual(got, []GoalAuditRecord{newer}) {
		t.Fatal("retention kept an expired reply or removed a newer one")
	}
	files, err := os.ReadDir(dir)
	if err != nil || len(files) != 1 {
		t.Fatal("interrupted write was not removed", err)
	}
}

type auditFunc func(GoalAuditRecord) error

func (f auditFunc) Save(r GoalAuditRecord) error { return f(r) }

func TestGoalAuditKeepsReplacementTaskReply(t *testing.T) {
	s, err := OpenGoalAudit(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r := GoalAuditRecord{MessageID: "same-message", TaskID: "first-task", CreatedAt: time.Now(), Reply: "first reply"}
	if err := s.Save(r); err != nil {
		t.Fatal(err)
	}
	// A provider can forget the message ID after deleting its session.
	r.TaskID, r.Reply = "replacement-task", "replacement reply"
	if err := s.Save(r); err != nil {
		t.Fatal(err)
	}
	if records := auditRecords(t, s); len(records) != 2 || records[0].Reply == records[1].Reply {
		t.Fatal("a replacement task's reply must not be lost to deduplication")
	}
}

func TestGoalReplySavedBeforeSessionDeletion(t *testing.T) {
	for _, reply := range []string{goalReply(`{"criteria": [], "overall": "met"}`), "PRIVATE invalid answer"} {
		t.Run(reply[:8], func(t *testing.T) {
			h, _ := issueHarness(t)
			withComments(h)
			h.k.goal = reply
			cleaner := &fakeCleaner{}
			h.cfg.Cleaner = cleaner
			store := h.cfg.GoalAudit.(*FileGoalAudit)
			h.cfg.GoalAudit = auditFunc(func(r GoalAuditRecord) error {
				if len(cleaner.deletions()) != 0 {
					t.Error("session deleted before audit commit")
				}
				return store.Save(r)
			})
			e, ctx, _ := h.start()
			h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
			e.Wait()
			records := auditRecords(t, store)
			if len(records) != 1 || records[0].Reply != reply || len(cleaner.deletions()) != 1 {
				t.Fatal("valid and malformed replies must be kept before deleting the session")
			}
			if strings.Contains(published(t, h)+h.log.String(), "PRIVATE") {
				t.Fatal("private reply left the audit store")
			}
		})
	}
}

func TestGoalAuditFailureKeepsSessionAndCannotPass(t *testing.T) {
	for _, unavailable := range []bool{false, true} {
		h, _ := issueHarness(t)
		withComments(h)
		cleaner := &fakeCleaner{}
		h.cfg.Cleaner = cleaner
		h.cfg.GoalAudit = auditFunc(func(GoalAuditRecord) error { return errors.New("PRIVATE storage error") })
		if unavailable {
			h.cfg.GoalAudit = nil
		}
		e, ctx, _ := h.start()
		h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
		e.Wait()
		c := h.gh.check(shaA)
		if c.Conclusion != ConclusionFailure || !strings.Contains(c.Summary, "goal check unavailable") || len(cleaner.deletions()) != 0 {
			t.Fatal("missing audit must keep the session and hold the goal check")
		}
		if strings.Contains(published(t, h)+h.log.String(), "PRIVATE") {
			t.Fatal("storage error leaked")
		}
	}
}
