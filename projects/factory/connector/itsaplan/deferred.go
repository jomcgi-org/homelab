package itsaplan

import (
	"context"
	"net/http"
	"net/url"
	"strconv"
)

func (c *Client) Project(ctx context.Context, key string) (*Project, error) {
	var out Project
	err := c.do(ctx, c.Coordinator, http.MethodGet, "/projects/"+url.PathEscape(key), nil, &out)
	return &out, err
}

// ProjectIssues reads the complete active board, including dates and label IDs.
func (c *Client) ProjectIssues(ctx context.Context, key string) ([]Issue, error) {
	var out struct {
		Issues []Issue `json:"issues"`
	}
	err := c.do(ctx, c.Coordinator, http.MethodGet, "/projects/"+url.PathEscape(key)+"/issues/board", nil, &out)
	return out.Issues, err
}

type DeferredRequest struct {
	ProjectID      int64  `json:"projectId"`
	StartDate      string `json:"startDate"`
	DelegateUserID string `json:"delegateUserId"`
}

type DeferredReceipt struct {
	Key   string `json:"key"`
	RunID int64  `json:"runId"`
}

// StartDeferred uses the chart-mounted v1.10.0 transaction adapter. The issue
// and date are the idempotency key; every retry must carry the same delegate.
func (c *Client) StartDeferred(ctx context.Context, id int64, request DeferredRequest) (*DeferredReceipt, error) {
	var out DeferredReceipt
	err := c.do(ctx, c.Coordinator, http.MethodPost, "/issues/"+strconv.FormatInt(id, 10)+"/factory-deferred-start", request, &out)
	return &out, err
}
