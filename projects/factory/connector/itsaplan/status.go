package itsaplan

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
)

// IssueSummary deliberately cannot hold an issue body or its activity.
type IssueSummary struct {
	ID             int64   `json:"id"`
	Identifier     string  `json:"identifier"`
	Title          string  `json:"title"`
	Priority       *string `json:"priority"`
	CreatedAt      string  `json:"createdAt"`
	ColumnID       int64   `json:"columnId"`
	AssigneeUserID *string `json:"assigneeUserId"`
	DelegateUserID *string `json:"delegateUserId"`
	Archived       bool    `json:"archived"`
}

// NeedsYou reads the bound project's scaffold and at most 500 assigned issues
// as the coordinator. v1.10.0 has no null-delegate or open-state list filter,
// so those predicates are checked locally. A full page fails rather than
// presenting an incomplete count as all of Joe's tasks.
// The pinned upstream routes and schemas are linked in factory/ARCHITECTURE.md,
// "Factory front page". Resolve the member each time instead of pinning a user ID.
func (c *Client) NeedsYou(ctx context.Context, project, username string) ([]IssueSummary, error) {
	var scaffold struct {
		Columns   []Column `json:"columns"`
		Assignees []struct {
			UserID   string `json:"userId"`
			Username string `json:"username"`
			Kind     string `json:"kind"`
		} `json:"assignees"`
	}
	path := "/projects/" + url.PathEscape(project)
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &scaffold); err != nil {
		return nil, err
	}
	userID := ""
	for _, a := range scaffold.Assignees {
		if a.Username == username && a.Kind == "member" {
			userID = a.UserID
		}
	}
	if userID == "" {
		return nil, fmt.Errorf("itsaplan: task owner is not a project member")
	}
	open := map[int64]bool{}
	for _, col := range scaffold.Columns {
		open[col.ID] = col.StateType == "backlog" || col.StateType == "unstarted" || col.StateType == "started"
	}
	q := url.Values{"assigneeUserId": {userID}, "limit": {"500"}, "includeArchived": {"false"}}
	var issues []IssueSummary
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path+"/issues?"+q.Encode(), nil, &issues); err != nil {
		return nil, err
	}
	if len(issues) >= 500 {
		return nil, fmt.Errorf("itsaplan: assigned issue listing reached its limit")
	}
	out := make([]IssueSummary, 0, len(issues))
	for _, issue := range issues {
		if issue.AssigneeUserID != nil && *issue.AssigneeUserID == userID && issue.DelegateUserID == nil && !issue.Archived && open[issue.ColumnID] {
			out = append(out, issue)
		}
	}
	return out, nil
}

// GetIssueSummary reads only the fields used by the factory front page.
func (c *Client) GetIssueSummary(ctx context.Context, id int64) (*IssueSummary, error) {
	var out IssueSummary
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/"+strconv.FormatInt(id, 10), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
