package itsaplan

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
)

type NewIssue struct {
	AssigneeUserID string `json:"assigneeUserId,omitempty"`
	DelegateUserID string `json:"delegateUserId,omitempty"`
	Priority       string `json:"priority,omitempty"`
	Title          string `json:"title"`
	Description    string `json:"description"`
	ColumnID       int64  `json:"columnId"`
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

type IssueUpdate struct {
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
