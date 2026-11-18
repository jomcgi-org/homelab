package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
)

func (c *Client) GetIssueByNumber(ctx context.Context, project string, number int64) (*Issue, error) {
	var out Issue
	path := "/projects/" + url.PathEscape(project) + "/issues/" + strconv.FormatInt(number, 10)
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	if out.ID <= 0 || out.Identifier != project+"-"+strconv.FormatInt(number, 10) {
		return nil, errors.New("itsaplan: invalid or moved issue")
	}
	return &out, nil
}

// FindIssue resolves a decision link without fetching another issue body. The
// v1.10.0 search ranks an exact identifier first; still require an exact match.
func (c *Client) FindIssue(ctx context.Context, identifier string) (*Issue, error) {
	var out []Issue
	q := url.Values{"q": {identifier}, "limit": {"1"}, "perProjectLimit": {"1"}}
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/search?"+q.Encode(), nil, &out); err != nil {
		return nil, err
	}
	if len(out) != 1 || out[0].Identifier != identifier || out[0].ID <= 0 {
		return nil, ErrNotFound
	}
	// Search returns metadata only, never a second issue description.
	out[0].Description = ""
	return &out[0], nil
}

type FeedItem struct {
	ID          int64           `json:"id"`
	IssueID     int64           `json:"issueId"`
	Kind        string          `json:"kind"`
	Action      string          `json:"action"`
	ActorUserID string          `json:"actorUserId"`
	Body        string          `json:"body"`
	EditedAt    json.RawMessage `json:"editedAt"`
	Payload     struct {
		From *struct {
			Value *string `json:"value"`
		} `json:"from"`
		To *struct {
			Value *string `json:"value"`
		} `json:"to"`
	} `json:"payload"`
}

// OriginalComment requires the API's explicit null stamp. An edited comment
// retains its original author, and the API supplies no authenticated editor.
func (f *FeedItem) OriginalComment() bool {
	return string(f.EditedAt) == "null"
}

// IssueTextAuthors authenticates current text against immutable change-log
// snapshots, never against the issue's creator. Creation events have no text;
// absence of a snapshot therefore grants no authority. Read newest first so an
// older trusted value cannot mask a later untrusted edit of the same field.
func (c *Client) IssueTextAuthors(ctx context.Context, issue *Issue) ([]string, error) {
	q := url.Values{"filter": {"history"}, "order": {"desc"}, "limit": {"100"}}
	want := map[string]string{"title": issue.Title, "description": issue.Description}
	seen := map[string]bool{}
	authors := map[string]string{}
	for range 10 {
		var out feedPage
		path := "/issues/" + strconv.FormatInt(issue.ID, 10) + "/feed?" + q.Encode()
		if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &out); err != nil {
			return nil, err
		}
		for _, item := range out.Items {
			value, relevant := want[item.Action]
			if !relevant || seen[item.Action] || item.Kind != "activity" || item.IssueID != issue.ID {
				continue
			}
			seen[item.Action] = true
			if item.Payload.To != nil && item.Payload.To.Value != nil && *item.Payload.To.Value == value {
				authors[item.Action] = item.ActorUserID
			}
		}
		if len(seen) == 2 || out.NextCursor == nil {
			return []string{authors["title"], authors["description"]}, nil
		}
		cursor, err := json.Marshal(out.NextCursor)
		if err != nil {
			return nil, err
		}
		q.Set("cursor", string(cursor))
	}
	// The bounded history does not establish the remaining fields' provenance.
	return []string{authors["title"], authors["description"]}, nil
}

// VisitDescriptions reads immutable description snapshots, newest first,
// stopping when visit recognizes the revision it needs.
func (c *Client) VisitDescriptions(ctx context.Context, id int64, visit func(string) bool) error {
	q := url.Values{"filter": {"history"}, "order": {"desc"}, "limit": {"100"}}
	for range 10 {
		var out feedPage
		path := "/issues/" + strconv.FormatInt(id, 10) + "/feed?" + q.Encode()
		if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &out); err != nil {
			return err
		}
		for _, item := range out.Items {
			if item.IssueID != id || item.Kind != "activity" || item.Action != "description" {
				continue
			}
			if item.Payload.To != nil && item.Payload.To.Value != nil && visit(*item.Payload.To.Value) {
				return nil
			}
			if item.Payload.From != nil && item.Payload.From.Value != nil && visit(*item.Payload.From.Value) {
				return nil
			}
		}
		if out.NextCursor == nil {
			break
		}
		cursor, err := json.Marshal(out.NextCursor)
		if err != nil {
			return err
		}
		q.Set("cursor", string(cursor))
	}
	return nil
}

type feedCursor struct {
	TS string `json:"ts"`
	ID int64  `json:"id"`
}

type feedPage struct {
	Items      []FeedItem  `json:"items"`
	NextCursor *feedCursor `json:"nextCursor"`
}

// IssueAuthor reads the creation event. Issues themselves have no author field
// in v1.10.0. A missing creation event grants no authority.
func (c *Client) IssueAuthor(ctx context.Context, id int64) (string, error) {
	var out feedPage
	path := "/issues/" + strconv.FormatInt(id, 10) + "/feed?filter=history&order=asc&limit=1"
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &out); err != nil {
		return "", err
	}
	if len(out.Items) != 1 || out.Items[0].IssueID != id || out.Items[0].Kind != "activity" || out.Items[0].Action != "created" {
		return "", errors.New("itsaplan: no creation event")
	}
	return out.Items[0].ActorUserID, nil
}

// LinkedComment reads at most ten feed pages. v1.10.0 has no GET /comments/:id;
// comments (including replies) can only be read through their issue's feed.
// Missing comments and exhaustion both return no evidence.
func (c *Client) LinkedComment(ctx context.Context, issueID, commentID int64) (*FeedItem, error) {
	q := url.Values{"filter": {"comments"}, "order": {"desc"}, "limit": {"100"}}
	for range 10 {
		var out feedPage
		path := "/issues/" + strconv.FormatInt(issueID, 10) + "/feed?" + q.Encode()
		if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &out); err != nil {
			return nil, err
		}
		for _, item := range out.Items {
			if item.ID == commentID && item.IssueID == issueID && item.Kind == "comment" {
				return &item, nil
			}
		}
		if out.NextCursor == nil {
			return nil, ErrNotFound
		}
		// Cursor timestamps come from the API, never from the PR.
		cursor, err := json.Marshal(out.NextCursor)
		if err != nil {
			return nil, err
		}
		q.Set("cursor", string(cursor))
	}
	return nil, errors.New("itsaplan: comment lookup exceeded feed bound")
}
