package github

import (
	"context"
	"fmt"
	"maps"
	"slices"
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

// StatusBatchSize caps each query at 75 connections, all with last: 1.
// A query costs one primary GraphQL point under GitHub's connection formula.
const StatusBatchSize = 25

const statusFields = `
      number url reviewDecision
      mergeQueueEntry { state }
      commits(last: 1) { nodes { commit {
        statusCheckRollup { state }
        checkSuites(last: 1, filterBy: {appId: $appId}) @include(if: $hasReviewer) {
          nodes { checkRuns(last: 1, filterBy: {checkName: $check}) { nodes { status conclusion } } }
        }
      } } }
`

type statusPullRequest struct {
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
}

// StatusesForLanding batches display reads so 42 live PRs cost two queries per refresh,
// rather than 42. Inputs are sorted and deduplicated to keep retry IDs stable.
// The factory review check takes precedence over human reviews when configured.
func (c *Client) StatusesForLanding(ctx context.Context, repo string, numbers []int, reviewer CheckRunFilter) (map[int]*PRStatus, error) {
	if len(numbers) > 100 {
		return nil, fmt.Errorf("github: status listing exceeds 100 PRs")
	}
	numbers = slices.Clone(numbers)
	slices.Sort(numbers)
	numbers = slices.Compact(numbers)
	for _, number := range numbers {
		if number <= 0 {
			return nil, fmt.Errorf("github: invalid status PR number")
		}
	}
	// At most four requests share the refresh deadline. A slow batch does
	// not delay the start of the other batches.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type batchResult struct {
		statuses map[int]*PRStatus
		err      error
	}
	batches := (len(numbers) + StatusBatchSize - 1) / StatusBatchSize
	replies := make(chan batchResult, batches)
	for batch := range slices.Chunk(numbers, StatusBatchSize) {
		go func() {
			statuses, err := c.landingStatusBatch(ctx, repo, batch, reviewer)
			replies <- batchResult{statuses: statuses, err: err}
		}()
	}
	result := make(map[int]*PRStatus, len(numbers))
	for range batches {
		reply := <-replies
		if reply.err != nil {
			return nil, reply.err
		}
		maps.Copy(result, reply.statuses)
	}
	return result, nil
}

func (c *Client) landingStatusBatch(ctx context.Context, repo string, numbers []int, reviewer CheckRunFilter) (map[int]*PRStatus, error) {
	owner, name, _ := strings.Cut(repo, "/")
	var query strings.Builder
	query.WriteString(`query($owner: String!, $name: String!, $hasReviewer: Boolean!, $appId: Int!, $check: String!) {
          repository(owner: $owner, name: $name) {`)
	for _, number := range numbers {
		fmt.Fprintf(&query, "p%d: pullRequest(number: %d) {%s}\n", number, number, statusFields)
	}
	query.WriteString("}}")
	var out struct {
		Repository map[string]*statusPullRequest `json:"repository"`
	}
	vars := map[string]any{
		"owner": owner, "name": name,
		"hasReviewer": reviewer.AppID != 0 && reviewer.Name != "", "appId": reviewer.AppID, "check": reviewer.Name,
	}
	if err := c.graphql(ctx, repo, query.String(), vars, &out); err != nil {
		return nil, err
	}
	result := make(map[int]*PRStatus, len(numbers))
	for _, number := range numbers {
		p := out.Repository[fmt.Sprintf("p%d", number)]
		if p == nil || p.Number != number {
			return nil, ErrNotFound
		}
		result[number] = p.summary(reviewer)
	}
	return result, nil
}

func (p *statusPullRequest) summary(reviewer CheckRunFilter) *PRStatus {
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
	return s
}
