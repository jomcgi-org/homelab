package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type reviewingIssueMover interface {
	MarkIssueReviewing(context.Context, int64, string) (bool, error)
}

type reviewingIssue struct {
	WorkID  int64  `json:"workId"`
	Head    string `json:"head"`
	CheckID int64  `json:"checkId"`
}

func pendingReviewerCheck(check *Check, repo string, number int, head string, checks []github.CheckRun) int64 {
	if check == nil || check.AppID == 0 || head == "" {
		return 0
	}
	externalID := fmt.Sprintf("factory-review/%s/%d/%s", repo, number, head)
	var latest github.CheckRun
	for _, run := range checks {
		if run.HeadSHA == head && run.AppID == check.AppID && run.Name == check.Name && run.ExternalID == externalID && run.ID > latest.ID {
			latest = run
		}
	}
	if latest.Status == "queued" || latest.Status == "in_progress" {
		return latest.ID
	}
	return 0
}

func (e *Engine) queueIssueReviewing(ctx context.Context, wi *store.WorkItem, b *Binding, pr *github.PullRequestDetail, checks []github.CheckRun) error {
	if b.ItsaplanProject != "HOME" || wi.State != store.WorkReview || pr.Draft || b.Review.held(pr.Labels) != "" {
		return nil
	}
	if _, ok := e.its.(reviewingIssueMover); !ok {
		return nil
	}
	check := pendingReviewerCheck(b.Review.ReviewerCheck, b.repo(), pr.Number, pr.HeadSHA, checks)
	if check == 0 {
		return nil
	}
	key := fmt.Sprintf("issue-reviewing/%d/%s/%d", wi.ID, pr.HeadSHA, check)
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.Enqueue(ctx, tx, store.OutboxIssueReviewing, key, 0, "", reviewingIssue{
			WorkID: wi.ID, Head: pr.HeadSHA, CheckID: check,
		})
	})
}

func (e *Engine) deliverIssueReviewing(ctx context.Context, it *store.OutboxItem) error {
	finish := func() error {
		return e.w.Tx(ctx, func(tx *sql.Tx) error {
			return e.st.OutboxDelivered(ctx, tx, it.ID)
		})
	}
	var pending reviewingIssue
	if err := json.Unmarshal(it.Payload, &pending); err != nil {
		return e.abandonItem(ctx, it, err)
	}
	work, err := e.st.GetWorkItem(ctx, pending.WorkID)
	if errors.Is(err, sql.ErrNoRows) {
		return finish()
	}
	if err != nil {
		return err
	}
	b := e.binding(work.BindingName)
	mover, ok := e.its.(reviewingIssueMover)
	if b == nil || b.ItsaplanProject != "HOME" || !ok || work.State != store.WorkReview {
		return finish()
	}
	pr, err := e.st.PullRequestFor(ctx, work.ID)
	if err != nil {
		return err
	}
	if pr == nil || pr.State != "open" || pr.HeadSHA != pending.Head {
		return finish()
	}

	if err := e.BeginStatusRead(ctx, it.DedupeKey+"/pr"); err != nil {
		return err
	}
	current, err := e.deliveryDetail(ctx, b.repo(), pr.Number)
	if errors.Is(err, github.ErrNotFound) || errors.Is(err, errDeliverySource) {
		return finish()
	}
	if err != nil {
		return err
	}
	if current.HeadSHA != pending.Head || current.State != "open" || current.Draft ||
		current.BaseRef != b.BaseBranch || b.Review.held(current.Labels) != "" {
		return finish()
	}
	if err := e.BeginStatusRead(ctx, it.DedupeKey+"/checks"); err != nil {
		return err
	}
	checks, err := e.gh.CheckRuns(ctx, b.repo(), current.HeadSHA)
	if err != nil {
		return err
	}
	if pendingReviewerCheck(b.Review.ReviewerCheck, b.repo(), pr.Number, pending.Head, checks) != pending.CheckID {
		return finish()
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE outbox SET send_started_at = now() WHERE id = $1`, it.ID)
		return err
	}); err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	settled, err := mover.MarkIssueReviewing(ctx, work.IssueID, pending.Head)
	if err != nil {
		if e.refused(ctx, it, err) {
			return e.abandonItem(ctx, it, err)
		}
		return err
	}
	if !settled {
		return errors.New("issue is not ready to enter Reviewing")
	}
	return finish()
}
