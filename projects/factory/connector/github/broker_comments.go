package github

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// DefaultCommentsPath is the broker endpoint that writes the reviewer's PR
// conversation comments.
const DefaultCommentsPath = "/github/reviewer-comments"

// BrokerComments lists, creates and edits the reviewer's PR conversation
// comments through the broker. GitHub needs pull_requests write for a PR
// comment, which could approve, so the comment token stays in the broker
// and this client never holds it.
type BrokerComments struct {
	// URL is the broker's base URL.
	URL string
	// Path is the endpoint, DefaultCommentsPath when empty.
	Path   string
	Bearer Bearer
	HTTP   *http.Client
}

type brokerComment struct {
	ID     int64  `json:"id"`
	Author string `json:"author"`
	Body   string `json:"body"`
}

func (c brokerComment) comment() Comment {
	return Comment{ID: c.ID, Author: c.Author, Body: c.Body}
}

// IssueComments lists a PR's conversation comments, oldest first, page by
// page. complete is false when MaxCommentPages pages were read and GitHub
// named a next page.
func (b *BrokerComments) IssueComments(ctx context.Context, repo string, number int) (comments []Comment, complete bool, err error) {
	for page := 1; page <= MaxCommentPages; page++ {
		var out struct {
			Comments []brokerComment `json:"comments"`
			Next     bool            `json:"next"`
		}
		q := url.Values{"repository": {repo}, "number": {strconv.Itoa(number)}, "page": {strconv.Itoa(page)}}
		if err := b.do(ctx, http.MethodGet, "?"+q.Encode(), nil, &out); err != nil {
			return nil, false, err
		}
		for _, c := range out.Comments {
			comments = append(comments, c.comment())
		}
		if !out.Next {
			return comments, true, nil
		}
	}
	return comments, false, nil
}

// CreateIssueComment posts a conversation comment on a pull request.
func (b *BrokerComments) CreateIssueComment(ctx context.Context, repo string, number int, body string) (*Comment, error) {
	var out brokerComment
	in := map[string]any{"repository": repo, "number": number, "body": body}
	if err := b.do(ctx, http.MethodPost, "", in, &out); err != nil {
		return nil, err
	}
	c := out.comment()
	return &c, nil
}

// UpdateIssueComment replaces the body of a comment the reviewer wrote. A
// deleted comment is ErrNotFound.
func (b *BrokerComments) UpdateIssueComment(ctx context.Context, repo string, id int64, body string) (*Comment, error) {
	var out brokerComment
	in := map[string]any{"repository": repo, "id": id, "body": body}
	if err := b.do(ctx, http.MethodPatch, "", in, &out); err != nil {
		return nil, err
	}
	c := out.comment()
	return &c, nil
}

func (b *BrokerComments) do(ctx context.Context, method, query string, in, out any) error {
	bearer, err := b.Bearer.Token()
	if err != nil {
		return err
	}
	path := b.Path
	if path == "" {
		path = DefaultCommentsPath
	}
	var rd io.Reader
	if in != nil {
		data, err := json.Marshal(in)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimSuffix(b.URL, "/")+path+query, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	hc := b.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return fmt.Errorf("broker: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		var e struct {
			Code string `json:"factory_error"`
		}
		_ = json.Unmarshal(data, &e)
		if resp.StatusCode == http.StatusNotFound && e.Code == "not_found" {
			return ErrNotFound
		}
		return fmt.Errorf("broker: %s %s: HTTP %d: %.200s", method, path, resp.StatusCode, data)
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("broker: decode %s answer: %w", path, err)
	}
	return nil
}
