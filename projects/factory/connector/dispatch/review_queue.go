package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// IssueReview reports only the current PR of the requested work item.
type IssueReview struct {
	PullRequest int       `json:"pullRequest"`
	Head        string    `json:"head"`
	Phase       string    `json:"phase"`
	Position    int       `json:"position,omitempty"`
	ObservedAt  time.Time `json:"observedAt"`
	Complete    bool      `json:"complete"`
}

// IssueReviewQueue joins the reviewer snapshot to a work receipt by repository,
// PR and head. A stale snapshot cannot attribute activity to a newer head.
func (e *Engine) IssueReviewQueue(ctx context.Context, issueID int64, base string, client *http.Client) (*IssueReview, error) {
	work, err := e.st.WorkItemByIssue(ctx, issueID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	pr, err := e.st.PullRequestFor(ctx, work.ID)
	if err != nil || pr == nil || pr.State != "open" {
		return nil, err
	}
	binding := e.binding(work.BindingName)
	if binding == nil {
		return nil, errors.New("review queue binding unavailable")
	}
	messageID := fmt.Sprintf("review-queue/%d/%s", pr.ID, pr.HeadSHA)
	if err := e.w.RecordDeliveryRead(ctx, pr.ID, messageID, "review-queue", pr.HeadSHA); err != nil {
		return nil, err
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(base, "/")+"/v1/review-queue", nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("review queue unavailable")
	}
	type entry struct {
		Repository  string `json:"repository"`
		PullRequest int    `json:"pullRequest"`
		Head        string `json:"head"`
		Position    int    `json:"position"`
	}
	var snapshot struct {
		ObservedAt time.Time `json:"observedAt"`
		Complete   bool      `json:"complete"`
		QuotaHeld  []entry   `json:"quotaHeld"`
		Waiting    []entry   `json:"waiting"`
		Active     []entry   `json:"active"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&snapshot); err != nil {
		return nil, err
	}
	matches := func(item entry) bool {
		return item.Repository == binding.repo() && item.PullRequest == pr.Number && item.Head == pr.HeadSHA
	}
	answer := IssueReview{PullRequest: pr.Number, Head: pr.HeadSHA, ObservedAt: snapshot.ObservedAt, Complete: snapshot.Complete}
	for _, item := range snapshot.Active {
		if matches(item) {
			answer.Phase = "reviewing"
			return &answer, nil
		}
	}
	for _, item := range snapshot.Waiting {
		if matches(item) {
			answer.Phase = "queued"
			if snapshot.Complete {
				answer.Position = item.Position
			}
			return &answer, nil
		}
	}
	for _, item := range snapshot.QuotaHeld {
		if matches(item) {
			answer.Phase = "quota-held"
			return &answer, nil
		}
	}
	if !snapshot.Complete {
		return nil, errors.New("review queue observation incomplete")
	}
	return nil, nil
}
