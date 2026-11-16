package itsaplan

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
)

// Project includes assignable users and effective issue-creation permission.
type Project struct {
	Permissions struct {
		WorkItems struct {
			Create bool `json:"create"`
		} `json:"work_items"`
	} `json:"permissions"`
	Columns   []Column   `json:"columns"`
	Assignees []Assignee `json:"assignees"`
}

type Assignee struct {
	UserID   string `json:"userId"`
	Username string `json:"username"`
	Kind     string `json:"kind"`
}

func (c *Client) GetProject(ctx context.Context, key string) (*Project, error) {
	var out Project
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/projects/"+url.PathEscape(key), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GateIssueWrite is a full, idempotent assignment and state update. An explicit
// null delegate prevents an issue for a human from triggering a coding run.
type GateIssueWrite struct {
	Title          string  `json:"title"`
	Description    string  `json:"description"`
	ColumnID       int64   `json:"columnId"`
	AssigneeUserID string  `json:"assigneeUserId"`
	DelegateUserID *string `json:"delegateUserId"`
}

func (c *Client) CreateGateIssue(ctx context.Context, project string, body GateIssueWrite) (*Issue, error) {
	var out Issue
	if err := c.do(ctx, c.Coordinator, http.MethodPost, "/projects/"+url.PathEscape(project)+"/issues", body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) UpdateGateIssue(ctx context.Context, id int64, body GateIssueWrite) error {
	return c.do(ctx, c.Coordinator, http.MethodPatch, "/issues/"+strconv.FormatInt(id, 10), body, nil)
}

// SearchGateIssues searches archived issues too. Hits omit descriptions; the
// dispatcher reads each hit under its attempt protocol and checks the marker.
func (c *Client) SearchGateIssues(ctx context.Context, project, marker string) ([]Issue, error) {
	q := url.Values{"q": {marker}, "limit": {"500"}}
	var hits []Issue
	if err := c.do(ctx, c.Coordinator, http.MethodGet,
		"/projects/"+url.PathEscape(project)+"/issues/search?"+q.Encode(), nil, &hits); err != nil {
		return nil, err
	}
	if len(hits) >= 500 {
		return nil, fmt.Errorf("itsaplan: gate issue search reached its limit")
	}
	return hits, nil
}
