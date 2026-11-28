package itsaplan

import (
	"context"
	"errors"
	"net/http"
	"net/url"
)

// FileRequest creates one issue per key, as the coordinator. It uses the
// keyed follow-up route (chart/files/itsaplan/factory.ts): a replay with
// the same key returns the first issue's ID and creates nothing.
func (c *Client) FileRequest(ctx context.Context, project, key string, issue NewIssue) (int64, error) {
	request := struct {
		Key         string `json:"key"`
		Title       string `json:"title"`
		Description string `json:"description"`
		ColumnID    int64  `json:"columnId"`
	}{key, issue.Title, issue.Description, issue.ColumnID}
	var receipt struct {
		ID int64 `json:"id"`
	}
	if err := c.do(ctx, c.Coordinator, http.MethodPost, "/projects/"+url.PathEscape(project)+"/factory-review-followups", request, &receipt); err != nil {
		return 0, err
	}
	if receipt.ID <= 0 {
		return 0, errors.New("itsaplan: request receipt lacks an issue ID")
	}
	return receipt.ID, nil
}
