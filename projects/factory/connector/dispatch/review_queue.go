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
	Reason      string    `json:"reason,omitempty"`
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
	snapshot, err := readReviewSnapshot(ctx, base, client)
	if err != nil {
		return nil, err
	}
	matches := func(item reviewSnapshotEntry) bool {
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
			answer.Reason = "Waiting for a review worker slot."
			if snapshot.Complete {
				answer.Position = item.Position
			}
			return &answer, nil
		}
	}
	for _, item := range snapshot.QuotaHeld {
		if matches(item) {
			answer.Phase = "quota-held"
			answer.Reason = "Waiting for review quota."
			return &answer, nil
		}
	}
	if !snapshot.Complete {
		return nil, errors.New("review queue observation incomplete")
	}
	return nil, nil
}

type reviewSnapshotEntry struct {
	Repository  string `json:"repository"`
	PullRequest int    `json:"pullRequest"`
	Head        string `json:"head"`
	Position    int    `json:"position"`
}

type reviewSnapshot struct {
	ObservedAt time.Time             `json:"observedAt"`
	Complete   bool                  `json:"complete"`
	Waiting    []reviewSnapshotEntry `json:"waiting"`
	Active     []reviewSnapshotEntry `json:"active"`
	QuotaHeld  []reviewSnapshotEntry `json:"quotaHeld"`
}

func readReviewSnapshot(ctx context.Context, base string, client *http.Client) (reviewSnapshot, error) {
	var snapshot reviewSnapshot
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(base, "/")+"/v1/review-queue", nil)
	if err != nil {
		return snapshot, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return snapshot, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return snapshot, errors.New("review queue unavailable")
	}
	err = json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&snapshot)
	return snapshot, err
}

type ProjectReviewItem struct {
	IssueID int64 `json:"issueId"`
	IssueReview
}

type ProjectReviewQueue struct {
	ObservedAt time.Time           `json:"observedAt"`
	Complete   bool                `json:"complete"`
	Items      []ProjectReviewItem `json:"items"`
}

// ProjectReviews reports observed reviewer activity on open PRs linked to
// factory work items. The API independently checks the issue's current project.
func (e *Engine) ProjectReviews(ctx context.Context, project, base string, client *http.Client) (ProjectReviewQueue, error) {
	reply := ProjectReviewQueue{Items: []ProjectReviewItem{}}
	repositories := make(map[string]string)
	for _, b := range e.cfg.Bindings {
		if b.ItsaplanProject == project {
			repositories[b.Name] = b.repo()
		}
	}
	if len(repositories) == 0 {
		return reply, nil
	}
	if err := e.BeginStatusRead(ctx, "project-review-queue/"+project); err != nil {
		return reply, err
	}
	snapshot, err := readReviewSnapshot(ctx, base, client)
	if err != nil {
		return reply, err
	}
	reply.ObservedAt, reply.Complete = snapshot.ObservedAt, snapshot.Complete

	type pullHead struct {
		repository string
		number     int
		head       string
	}
	current := make(map[pullHead]int64)
	rows, err := e.st.DB.QueryContext(ctx, `
		SELECT w.issue_id, w.binding_name, p.number, p.head_sha
		FROM work_item w JOIN pull_request p ON p.work_item_id = w.id
		JOIN repo_binding b ON b.name = w.binding_name
		WHERE p.state = 'open' AND b.itsaplan_project = $1`, project)
	if err != nil {
		return reply, err
	}
	defer rows.Close()
	for rows.Next() {
		var issueID int64
		var binding string
		var head pullHead
		if err := rows.Scan(&issueID, &binding, &head.number, &head.head); err != nil {
			return reply, err
		}
		if repo, ok := repositories[binding]; ok {
			head.repository = repo
			current[head] = issueID
		}
	}
	if err := rows.Err(); err != nil {
		return reply, err
	}
	add := func(entries []reviewSnapshotEntry, phase string) {
		for _, entry := range entries {
			head := pullHead{entry.Repository, entry.PullRequest, entry.Head}
			if issueID, ok := current[head]; ok {
				position := 0
				if snapshot.Complete {
					position = entry.Position
				}
				reply.Items = append(reply.Items, ProjectReviewItem{
					IssueID: issueID,
					IssueReview: IssueReview{
						PullRequest: entry.PullRequest, Head: entry.Head, Phase: phase,
						Position: position, ObservedAt: snapshot.ObservedAt, Complete: snapshot.Complete,
					},
				})
				delete(current, head)
			}
		}
	}
	add(snapshot.Active, "reviewing")
	add(snapshot.Waiting, "queued")
	add(snapshot.QuotaHeld, "quota-held")
	return reply, nil
}
