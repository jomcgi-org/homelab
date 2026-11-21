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

// CommentLookupTimeout gives the broker five minutes to read up to 100
// GitHub pages sequentially. Comment writes have a separate 30-second budget.
const CommentLookupTimeout = 5 * time.Minute

// maxBrokerCommentAnswer bounds a broker answer: one comment of at most
// 65536 characters at six bytes each when escaped, plus its other fields.
const maxBrokerCommentAnswer = 1 << 20

// BrokerComments finds, creates and edits the reviewer's PR conversation
// comment through the broker. GitHub needs pull_requests write for a PR
// comment, which could approve, so the comment token stays in the broker
// and this client never holds it.
type BrokerComments struct {
	// URL is the broker's base URL.
	URL string
	// Path is the endpoint, DefaultCommentsPath when empty.
	Path string
	// Prefix is the marker the reviewer's comment starts with. The broker
	// matches it and the bot login, so an older bot comment without it is
	// passed over.
	Prefix string
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

// IssueComments returns the reviewer's own comment on a PR, the oldest the
// broker's configured bot login wrote that starts with Prefix, as the only
// element, or none. The
// broker searches the PR's comments and stops at the first match, so this
// is one request. complete is false when the broker stopped at its bound
// with comments left unread.
func (b *BrokerComments) IssueComments(ctx context.Context, repo string, number int) (comments []Comment, complete bool, err error) {
	var out struct {
		Comment  *brokerComment `json:"comment"`
		Complete bool           `json:"complete"`
	}
	if b.Prefix == "" {
		return nil, false, fmt.Errorf("broker comments: no comment marker to find")
	}
	q := url.Values{"repository": {repo}, "number": {strconv.Itoa(number)}, "prefix": {b.Prefix}}
	if err := b.do(ctx, http.MethodGet, "?"+q.Encode(), nil, &out); err != nil {
		return nil, false, err
	}
	if out.Comment != nil {
		comments = append(comments, out.Comment.comment())
	}
	return comments, out.Complete, nil
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
		timeout := 30 * time.Second
		if method == http.MethodGet {
			timeout = CommentLookupTimeout
		}
		hc = &http.Client{Timeout: timeout}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return fmt.Errorf("broker: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxBrokerCommentAnswer+1))
	if err != nil {
		return err
	}
	if len(data) > maxBrokerCommentAnswer {
		return fmt.Errorf("broker: %s %s: answer over %d bytes", method, path, maxBrokerCommentAnswer)
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

// InlineFinding is a validated right-side diff comment. Key is the digest of the stable finding ID.
type InlineFinding struct {
	Key   string
	Path  string
	Start int
	End   int
	Body  string
}

func (b *BrokerComments) SyncInline(ctx context.Context, repo string, number int, head string, findings []InlineFinding, active []string, clearedAt map[string]string) (map[string]int64, error) {
	var links map[string]int64
	var fixed []string
	for key := range clearedAt {
		fixed = append(fixed, key)
	}
	err := b.do(ctx, http.MethodPost, "?inline=1", map[string]any{"repository": repo, "number": number, "head": head, "findings": findings, "active": active, "fixed": fixed, "clearedAt": clearedAt}, &links)
	return links, err
}
