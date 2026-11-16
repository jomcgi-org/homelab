package github

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// AppComment is conversation or inline evidence from a GitHub App. Nil
// Outdated means GitHub does not report it (conversation comments).
type AppComment struct {
	ID        int64
	NodeID    string
	Kind      string
	Body      string
	Path      string
	Line      int
	CommitSHA string
	Outdated  *bool
	CreatedAt time.Time
}

// PullRequestAppComments reads both comment collections, matching the bot
// login and, wherever REST reports it, performed_via_github_app.id. Bounds
// count all authors so an incomplete listing cannot look like no findings.
// Only the newest limit matching comments are returned, oldest first.
func (c *Client) PullRequestAppComments(ctx context.Context, repo string, number int, author string, appID int64, limit int) ([]AppComment, int, bool, error) {
	var comments []AppComment
	complete := true
	for _, kind := range []string{"issues", "pulls"} {
		for page := 1; page <= MaxCommentPages; page++ {
			var rows []struct {
				restComment
				NodeID         string    `json:"node_id"`
				Path           string    `json:"path"`
				Line           int       `json:"line"`
				OriginalLine   int       `json:"original_line"`
				OriginalCommit string    `json:"original_commit_id"`
				CreatedAt      time.Time `json:"created_at"`
			}
			q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
			path := repoPath(repo) + "/" + kind + "/" + strconv.Itoa(number) + "/comments?" + q.Encode()
			if err := c.do(ctx, repo, http.MethodGet, path, nil, &rows); err != nil {
				return nil, 0, false, err
			}
			for _, row := range rows {
				if !strings.EqualFold(row.User.Login, author) || (row.App != nil && row.App.ID != appID) {
					continue
				}
				cm := AppComment{
					ID: row.ID, NodeID: row.NodeID, Kind: kind, Body: row.Body,
					Path: row.Path, Line: row.Line, CommitSHA: row.OriginalCommit, CreatedAt: row.CreatedAt,
				}
				if cm.Line == 0 {
					cm.Line = row.OriginalLine
				}
				comments = append(comments, cm)
			}
			if len(rows) < MaxListed {
				break
			}
			if page == MaxCommentPages {
				complete = false
			}
		}
	}
	sort.SliceStable(comments, func(i, j int) bool {
		if !comments[i].CreatedAt.Equal(comments[j].CreatedAt) {
			return comments[i].CreatedAt.Before(comments[j].CreatedAt)
		}
		if comments[i].Kind != comments[j].Kind {
			return comments[i].Kind < comments[j].Kind
		}
		return comments[i].ID < comments[j].ID
	})
	omitted := max(0, len(comments)-limit)
	comments = comments[omitted:]
	var ids []string
	for _, cm := range comments {
		if cm.Kind == "pulls" {
			if cm.NodeID == "" {
				return nil, omitted, false, fmt.Errorf("inline comment has no node ID")
			}
			ids = append(ids, cm.NodeID)
		}
	}
	if len(ids) > 0 {
		// REST's null position also occurs for file comments. Read GitHub's
		// explicit outdated flag instead of inferring it from a missing line.
		var data struct {
			Nodes []struct {
				ID       string `json:"id"`
				Outdated *bool  `json:"outdated"`
			} `json:"nodes"`
		}
		if err := c.graphql(ctx, repo, `query AppCommentState($ids: [ID!]!) { nodes(ids: $ids) { ... on PullRequestReviewComment { id outdated } } }`, map[string]any{"ids": ids}, &data); err != nil {
			return nil, omitted, false, err
		}
		states := map[string]*bool{}
		for _, n := range data.Nodes {
			states[n.ID] = n.Outdated
		}
		for i := range comments {
			if comments[i].Kind == "pulls" {
				comments[i].Outdated = states[comments[i].NodeID]
				if comments[i].Outdated == nil {
					return nil, omitted, false, fmt.Errorf("inline comment state is unavailable")
				}
			}
		}
	}
	return comments, omitted, complete, nil
}
