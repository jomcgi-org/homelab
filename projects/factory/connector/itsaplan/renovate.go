package itsaplan

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
)

type NewIssue struct {
	Title       string `json:"title"`
	Description string `json:"description"`
	ColumnID    int64  `json:"columnId"`
}

// CreateIssue deliberately leaves delegation until the real identifier is
// available and the final description can name it. itsaplan has no create
// idempotency key; the caller journals the send and reconciles its marker.
func (c *Client) CreateIssue(ctx context.Context, project string, in NewIssue) (*Issue, error) {
	var out Issue
	if err := c.do(ctx, c.Coordinator, http.MethodPost, "/projects/"+url.PathEscape(project)+"/issues", in, &out); err != nil {
		return nil, err
	}
	if out.ID <= 0 || out.Identifier == "" {
		return nil, errors.New("itsaplan: create returned no issue identity")
	}
	return &out, nil
}

// CreateAssessment replays the tracker transaction keyed by parent and work item.
func (c *Client) CreateAssessment(ctx context.Context, parent, work int64) (*Issue, error) {
	var out Issue
	err := c.do(ctx, c.Coordinator, http.MethodPost, "/issues/"+strconv.FormatInt(parent, 10)+"/factory-assessment", map[string]string{"workId": strconv.FormatInt(work, 10)}, &out)
	if err == nil && (out.ID <= 0 || out.Identifier == "") {
		err = errors.New("assessment receipt lacks issue identity")
	}
	return &out, err
}

type IssueUpdate struct {
	StartDate      *string `json:"startDate,omitempty"`
	LabelIDs       []int64 `json:"labelIds,omitempty"`
	Description    *string `json:"description,omitempty"`
	DelegateUserID *string `json:"delegateUserId,omitempty"`
	ColumnID       *int64  `json:"columnId,omitempty"`
}

func (c *Client) UpdateIssue(ctx context.Context, id int64, in IssueUpdate) error {
	return c.do(ctx, c.Coordinator, http.MethodPatch, "/issues/"+strconv.FormatInt(id, 10), in, nil)
}

// SearchIssues searches descriptions too, including archived issues. Hits
// are only candidates: the caller reads each description for its exact marker.
func (c *Client) SearchIssues(ctx context.Context, project, marker string) ([]Issue, error) {
	var out []Issue
	q := url.Values{"q": {marker}, "limit": {"500"}}
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/projects/"+url.PathEscape(project)+"/issues/search?"+q.Encode(), nil, &out); err != nil {
		return nil, err
	}
	if out == nil || len(out) >= 500 {
		return nil, errors.New("itsaplan: incomplete issue search")
	}
	return out, nil
}
