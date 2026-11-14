package github

import (
	"context"
	"strings"
)

// PRStatus contains display metadata only. This query never requests bodies,
// reviews' text, check output or logs.
type PRStatus struct {
	Number int    `json:"number"`
	URL    string `json:"url"`
	CI     string `json:"ci"`
	Review string `json:"review"`
	Queue  string `json:"queue"`
}

const statusQuery = `query($owner: String!, $name: String!, $number: Int!, $hasReviewer: Boolean!, $appId: Int!, $check: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number url reviewDecision
      mergeQueueEntry { state }
      commits(last: 1) { nodes { commit {
        statusCheckRollup { state }
        checkSuites(last: 1, filterBy: {appId: $appId}) @include(if: $hasReviewer) {
          nodes { checkRuns(last: 1, filterBy: {checkName: $check}) { nodes { status conclusion } } }
        }
      } } }
    }
  }
}`

// Status reads one PR in one bounded GraphQL query. The factory review check
// takes precedence over GitHub's human review decision when configured.
func (c *Client) Status(ctx context.Context, repo string, number int, reviewer CheckRunFilter) (*PRStatus, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var out struct {
		Repository struct {
			PullRequest *struct {
				Number          int    `json:"number"`
				URL             string `json:"url"`
				ReviewDecision  string `json:"reviewDecision"`
				MergeQueueEntry *struct {
					State string `json:"state"`
				} `json:"mergeQueueEntry"`
				Commits struct {
					Nodes []struct {
						Commit struct {
							StatusCheckRollup *struct {
								State string `json:"state"`
							} `json:"statusCheckRollup"`
							CheckSuites struct {
								Nodes []struct {
									CheckRuns struct {
										Nodes []struct {
											Status     string `json:"status"`
											Conclusion string `json:"conclusion"`
										} `json:"nodes"`
									} `json:"checkRuns"`
								} `json:"nodes"`
							} `json:"checkSuites"`
						} `json:"commit"`
					} `json:"nodes"`
				} `json:"commits"`
			} `json:"pullRequest"`
		} `json:"repository"`
	}
	vars := map[string]any{"owner": owner, "name": name, "number": number,
		"hasReviewer": reviewer.AppID != 0 && reviewer.Name != "", "appId": reviewer.AppID, "check": reviewer.Name}
	if err := c.graphql(ctx, repo, statusQuery, vars, &out); err != nil {
		return nil, err
	}
	p := out.Repository.PullRequest
	if p == nil {
		return nil, ErrNotFound
	}
	s := &PRStatus{Number: p.Number, URL: p.URL, CI: "unknown", Review: "pending", Queue: "not_queued"}
	if p.ReviewDecision != "" && reviewer.AppID == 0 {
		s.Review = strings.ToLower(p.ReviewDecision)
	}
	if p.MergeQueueEntry != nil {
		s.Queue = strings.ToLower(p.MergeQueueEntry.State)
	}
	for _, n := range p.Commits.Nodes {
		if n.Commit.StatusCheckRollup != nil {
			s.CI = strings.ToLower(n.Commit.StatusCheckRollup.State)
		}
		for _, suite := range n.Commit.CheckSuites.Nodes {
			for _, check := range suite.CheckRuns.Nodes {
				s.Review = strings.ToLower(check.Status)
				if check.Conclusion != "" {
					s.Review = strings.ToLower(check.Conclusion)
				}
			}
		}
	}
	return s, nil
}
