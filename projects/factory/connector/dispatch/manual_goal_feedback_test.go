package dispatch

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/metrics"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type manualFeedbackFixture struct {
	Itsaplan
	GitHub
	project      itsaplan.Project
	issue        itsaplan.Issue
	comment      itsaplan.FeedItem
	pr           github.PullRequest
	check        github.CheckRun
	reasons      GoalReasons
	onRead       func()
	commentError error
	afterPost    error
	posted       []string
}

func (f *manualFeedbackFixture) GetProject(context.Context, string) (*itsaplan.Project, error) {
	return &f.project, nil
}

func (f *manualFeedbackFixture) GetIssueByNumber(context.Context, string, int64) (*itsaplan.Issue, error) {
	return &f.issue, nil
}

func (f *manualFeedbackFixture) LinkedComment(context.Context, int64, int64) (*itsaplan.FeedItem, error) {
	return &f.comment, nil
}

func (f *manualFeedbackFixture) RecentComments(context.Context, int64) ([]itsaplan.FeedItem, error) {
	return []itsaplan.FeedItem{f.comment}, nil
}

func (f *manualFeedbackFixture) GetPullRequest(context.Context, string, int) (*github.PullRequest, error) {
	return &f.pr, nil
}

func (f *manualFeedbackFixture) ListPullRequests(context.Context, string, string) ([]github.PullRequest, bool, error) {
	return []github.PullRequest{f.pr}, true, nil
}

func (f *manualFeedbackFixture) ListCheckRuns(context.Context, string, string, github.CheckRunFilter) ([]github.CheckRun, error) {
	return []github.CheckRun{f.check}, nil
}

func (f *manualFeedbackFixture) GoalReasons(context.Context, string, int, string) (*GoalReasons, error) {
	if f.onRead != nil {
		f.onRead()
	}
	return &f.reasons, nil
}

func (f *manualFeedbackFixture) Comment(_ context.Context, id int64, body string) error {
	if id != f.issue.ID {
		return fmt.Errorf("unexpected issue %d", id)
	}
	if f.commentError != nil {
		return f.commentError
	}
	f.posted = append(f.posted, body)
	return f.afterPost
}

func TestManualGoalFeedback(t *testing.T) {
	const repo, issueKey = "org/homelab", "HOME-9"
	head, digest := strings.Repeat("a", 40), strings.Repeat("b", 64)
	request := ManualGoalRequest{Repository: repo, Issue: issueKey, PullRequest: 42, Head: head, CommentID: 17}
	checkpoint := func(goalKey, evidence string, pending bool) string {
		state := fmt.Sprintf(`{"goal":{"head":%q,"digest":%q},"goalKey":%q,"issueDigest":%q,"issuesPending":%t}`,
			head, digest, goalKey, evidence, pending)
		return github.ReviewStatePrefix + base64.StdEncoding.EncodeToString([]byte(state)) + github.ReviewStateSuffix
	}
	for _, tc := range []struct {
		name string
		edit func(*manualFeedbackFixture)
	}{
		{"authorized request", nil},
		{"changed head", func(f *manualFeedbackFixture) { f.pr.HeadSHA = strings.Repeat("c", 40) }},
		{"changed check during audit read", func(f *manualFeedbackFixture) { f.onRead = func() { f.check.ID++ } }},
		{"changed evidence during audit read", func(f *manualFeedbackFixture) {
			f.onRead = func() { f.check.Text = checkpoint(issueKey, strings.Repeat("e", 64), true) }
		}},
		{"foreign check App", func(f *manualFeedbackFixture) { f.check.AppID++ }},
		{"same head in another PR", func(f *manualFeedbackFixture) { f.check.ExternalID = "factory-review/" + repo + "/43/" + head }},
		{"untrusted PR author", func(f *manualFeedbackFixture) { f.pr.Author = "contributor" }},
		{"edited request comment", func(f *manualFeedbackFixture) { f.comment.EditedAt = json.RawMessage(`"edited"`) }},
		{"untrusted requester", func(f *manualFeedbackFixture) { f.comment.ActorUserID = "outsider" }},
		{"wrong issue in checkpoint", func(f *manualFeedbackFixture) { f.check.Text = checkpoint("HOME-10", digest, false) }},
		{"wrong issue in private answer", func(f *manualFeedbackFixture) { f.reasons.Issue = "HOME-10" }},
		{"wrong evidence digest", func(f *manualFeedbackFixture) { f.reasons.Digest = strings.Repeat("e", 64) }},
		{"wrong reply revision", func(f *manualFeedbackFixture) { f.reasons.ReplyRevision++ }},
		{"replacement review pending", func(f *manualFeedbackFixture) { f.check.Status = "in_progress" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &manualFeedbackFixture{
				issue: itsaplan.Issue{ID: 23, ProjectID: 1, Identifier: issueKey},
				comment: itsaplan.FeedItem{
					ID: 17, IssueID: 23, ActorUserID: "operator", Kind: "comment",
					EditedAt: json.RawMessage("null"), Body: "Request goal feedback for " + repo + "#42@" + head,
				},
				pr: github.PullRequest{Number: 42, State: "open", Author: "maintainer", HeadSHA: head, HeadRepo: repo, BaseRef: "main"},
				check: github.CheckRun{
					ID: 12, Name: "factory review", AppID: 2, HeadSHA: head, Status: "completed",
					Conclusion: "failure", ExternalID: "factory-review/" + repo + "/42/" + head, Text: checkpoint(issueKey, digest, false),
				},
				reasons: GoalReasons{
					Repository: repo, PullRequest: 42, Head: head, Issue: issueKey, Digest: digest,
					Reasons: "Criterion 2 needs an exact dispatch receipt.",
				},
			}
			f.project.Project.ID, f.project.Project.Key = 1, "HOME"
			f.project.Assignees = []itsaplan.Assignee{{UserID: "operator", Kind: "member", Username: "joesessions"}}
			if tc.edit != nil {
				tc.edit(f)
			}
			queued := map[string]string{}
			h := ManualGoalFeedback{
				Plan: f, GitHub: f, Reasons: f,
				Bindings: []Binding{{
					Binding: store.Binding{Owner: "org", Repo: "homelab", BaseBranch: "main", ItsaplanProject: "HOME"},
					Review:  ReviewPolicy{TrustedReviewers: []string{"maintainer"}, ReviewerCheck: &Check{AppID: 2, Name: "factory review"}},
				}},
				Queue: func(_ context.Context, issue int64, key, body string, _ manualGoalProof) error {
					if issue != 23 {
						t.Fatalf("queued feedback on issue %d", issue)
					}
					queued[key] = body
					return nil
				},
			}
			for range 2 {
				err := h.Request(t.Context(), request)
				if tc.edit != nil && !errors.Is(err, ErrManualGoalUnavailable) || tc.edit == nil && err != nil {
					t.Fatalf("unexpected request result: %v", err)
				}
			}
			if tc.edit != nil && len(queued) != 0 || tc.edit == nil && len(queued) != 1 {
				t.Fatalf("unexpected private queue size %d", len(queued))
			}
			for _, body := range queued {
				if !strings.Contains(body, f.reasons.Reasons) || !strings.Contains(body, head) {
					t.Errorf("missing exact-head reason in private feedback: %q", body)
				}
			}
			if tc.edit == nil {
				if err := h.findRequests(t.Context(), &h.Bindings[0], f.pr); err != nil || len(queued) != 1 {
					t.Fatalf("comment-feed discovery did not reuse the delivery key: %v, %d queued", err, len(queued))
				}
				checkManualFeedbackDelivery(t, h.Bindings, f)
			}
		})
	}
}

func checkManualFeedbackDelivery(t *testing.T, bindings []Binding, f *manualFeedbackFixture) {
	t.Helper()
	ctx := t.Context()
	st, err := store.Open(pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer st.DB.Close()
	now := time.Now()
	engine := func(holder string) *Engine {
		w, err := st.TryAcquireWriter(ctx, holder)
		if w == nil || err != nil {
			t.Fatalf("acquire writer: %v", err)
		}
		e := &Engine{
			cfg: Config{Bindings: bindings, Now: func() time.Time { return now }},
			st:  st, w: w, its: f, gh: f, m: metrics.New(nil), log: slog.Default(),
		}
		e.EnableManualGoalFeedback(f, f, f)
		return e
	}
	poll := func(e *Engine) {
		t.Helper()
		if err := e.stepManualGoalFeedback(ctx); err != nil {
			t.Fatal(err)
		}
	}
	flush := func(e *Engine) {
		t.Helper()
		if err := e.flushOutbox(ctx); err != nil {
			t.Fatal(err)
		}
	}
	onlyPending := func() *store.OutboxItem {
		t.Helper()
		pending, err := st.PendingOutbox(ctx, now, 10)
		if err != nil || len(pending) != 1 {
			t.Fatalf("expected one queued feedback: %d, %v", len(pending), err)
		}
		if pending[0].Kind != manualGoalOutboxKind || pending[0].DedupeKey != "manual-goal-feedback/23/17" {
			t.Fatalf("queued private feedback with an unexpected kind or key: %s %s", pending[0].Kind, pending[0].DedupeKey)
		}
		return pending[0]
	}
	e := engine("first")
	defer e.w.Release()
	poll(e)
	original := onlyPending()
	head := f.pr.HeadSHA
	f.pr.HeadSHA = strings.Repeat("c", 40)
	flush(e)
	if len(f.posted) != 0 {
		t.Fatal("posted feedback after the head moved")
	}
	f.pr.HeadSHA = head
	f.check.ID++
	poll(e)
	recreated := onlyPending()
	if recreated.ID != original.ID {
		t.Fatal("refreshing unsent feedback allocated a new request")
	}
	// The previous binary abandons unknown kinds without sending a comment.
	if err := st.OutboxAbandon(ctx, st.DB, recreated.ID, "unknown kind "+recreated.Kind); err != nil {
		t.Fatal(err)
	}
	e.w.Release()
	retry := engine("after rollback")
	defer retry.w.Release()
	poll(retry)
	onlyPending()
	f.commentError = &itsaplan.HTTPError{Status: http.StatusTooManyRequests}
	if err := retry.flushOutbox(ctx); !errors.Is(err, f.commentError) || len(f.posted) != 0 {
		t.Fatalf("expected a definite refusal before posting: %v, %d replies", err, len(f.posted))
	}
	f.commentError = nil
	retry.w.Release()
	now = now.Add(2 * time.Second)
	next := engine("after restart")
	defer next.w.Release()
	poll(next)
	flush(next)
	if len(f.posted) != 1 || !strings.Contains(f.posted[0], f.reasons.Reasons) {
		t.Fatalf("expected one private reply after restart, got %d", len(f.posted))
	}
	originalRequest := f.comment.ID
	f.comment.ID = originalRequest + 1
	poll(next)
	flush(next)
	if len(f.posted) != 1 {
		t.Fatal("a second request posted the same assessment again")
	}

	f.check.ID++
	f.comment.ID = originalRequest
	poll(next)
	flush(next)
	if len(f.posted) != 1 {
		t.Fatal("a replacement check replied again to the original request")
	}

	f.comment.ID = originalRequest + 2
	f.reasons.Reasons = "The replacement review verified the dispatch receipt."
	f.afterPost = errors.New("response lost after comment creation")
	poll(next)
	flush(next)
	next.w.Release()
	resumed := engine("after uncertain response")
	defer resumed.w.Release()
	f.afterPost = nil
	poll(resumed)
	flush(resumed)
	if len(f.posted) != 2 || !strings.Contains(f.posted[1], f.reasons.Reasons) {
		t.Fatalf("expected one reply for each of two checks, got %d", len(f.posted))
	}
	f.comment.ID++
	poll(resumed)
	flush(resumed)
	if len(f.posted) != 2 {
		t.Fatal("another request repeated the replacement check after an uncertain response")
	}
}
