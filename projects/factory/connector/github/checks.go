package github

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// CheckRunFilter narrows a check run listing. Zero fields do not filter.
type CheckRunFilter struct {
	Name  string
	AppID int64
	// All includes older runs for recovery. A truncated result is an error.
	All bool
}

type restCheckRun struct {
	ID         int64      `json:"id"`
	NodeID     string     `json:"node_id"`
	Name       string     `json:"name"`
	Status     string     `json:"status"`
	Conclusion string     `json:"conclusion"`
	HeadSHA    string     `json:"head_sha"`
	ExternalID string     `json:"external_id"`
	StartedAt  *time.Time `json:"started_at"`
	HTMLURL    string     `json:"html_url"`
	App        struct {
		ID int64 `json:"id"`
	} `json:"app"`
	Output struct {
		Title   string `json:"title"`
		Summary string `json:"summary"`
		Text    string `json:"text"`
	} `json:"output"`
}

func (r restCheckRun) run() CheckRun {
	c := CheckRun{
		ID: r.ID, NodeID: r.NodeID, Name: r.Name, Status: r.Status, Conclusion: r.Conclusion, HeadSHA: r.HeadSHA,
		AppID: r.App.ID, ExternalID: r.ExternalID, URL: r.HTMLURL,
		Title: r.Output.Title, Summary: r.Output.Summary, Text: r.Output.Text,
	}
	if r.StartedAt != nil {
		c.StartedAt = *r.StartedAt
	}
	return c
}

// ListCheckRuns returns the latest matching runs or the full history when All is set.
func (c *Client) ListCheckRuns(ctx context.Context, repo, sha string, f CheckRunFilter) ([]CheckRun, error) {
	return c.ListCheckRunsWithJournal(ctx, repo, sha, f, nil)
}

// ListCheckRunsWithJournal calls before for each HTTP attempt, including retries.
func (c *Client) ListCheckRunsWithJournal(ctx context.Context, repo, sha string, f CheckRunFilter, before func(int) error) ([]CheckRun, error) {
	q := url.Values{"filter": {"latest"}, "per_page": {"100"}}
	if f.All {
		q.Set("filter", "all")
	}
	if f.Name != "" {
		q.Set("check_name", f.Name)
	}
	if f.AppID != 0 {
		q.Set("app_id", strconv.FormatInt(f.AppID, 10))
	}
	var runs []CheckRun
	seen := map[int64]bool{}
	for page := 1; ; page++ {
		if page > 1 {
			q.Set("page", strconv.Itoa(page))
		}
		var out struct {
			TotalCount int            `json:"total_count"`
			CheckRuns  []restCheckRun `json:"check_runs"`
		}
		path := repoPath(repo) + "/commits/" + url.PathEscape(sha) + "/check-runs?" + q.Encode()
		var guard func() error
		if before != nil {
			guard = func() error { return before(page) }
		}
		if err := c.send(ctx, repo, http.MethodGet, path, nil, &out, true, guard); err != nil {
			return nil, err
		}
		for _, r := range out.CheckRuns {
			if f.All && seen[r.ID] {
				return nil, fmt.Errorf("github: check history changed during pagination")
			}
			seen[r.ID] = true
			runs = append(runs, r.run())
		}
		if !f.All || len(runs) == out.TotalCount {
			return runs, nil
		}
		if len(out.CheckRuns) != 100 || len(runs) > out.TotalCount {
			return nil, fmt.Errorf("github: check history is incomplete")
		}
	}
}

// MaxAnnotations is how many annotations GitHub takes in one check run
// write.
const MaxAnnotations = 50

// Annotation marks a line range of a file in a check run's output.
type Annotation struct {
	Path      string `json:"path"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
	Level     string `json:"annotation_level"` // notice | warning | failure
	Title     string `json:"title,omitempty"`
	Message   string `json:"message"`
}

// CheckRunOutput is a check run's report. Title and Summary are required
// whenever an output is written.
type CheckRunOutput struct {
	Title       string       `json:"title"`
	Summary     string       `json:"summary"`
	Text        string       `json:"text,omitempty"`
	Annotations []Annotation `json:"annotations,omitempty"`
}

// CheckRunWrite creates or updates a check run; empty fields are not sent.
type CheckRunWrite struct {
	Name        string          `json:"name,omitempty"`
	HeadSHA     string          `json:"head_sha,omitempty"`
	ExternalID  string          `json:"external_id,omitempty"`
	DetailsURL  string          `json:"details_url,omitempty"`
	Status      string          `json:"status,omitempty"`
	Conclusion  string          `json:"conclusion,omitempty"`
	StartedAt   *time.Time      `json:"started_at,omitempty"`
	CompletedAt *time.Time      `json:"completed_at,omitempty"`
	Output      *CheckRunOutput `json:"output,omitempty"`
}

// CreateCheckRun creates a check run as the token's GitHub App.
func (c *Client) CreateCheckRun(ctx context.Context, repo string, w CheckRunWrite) (*CheckRun, error) {
	var out restCheckRun
	if err := c.do(ctx, repo, http.MethodPost, repoPath(repo)+"/check-runs", w, &out); err != nil {
		return nil, err
	}
	r := out.run()
	return &r, nil
}

// UpdateCheckRun changes a check run the token's GitHub App created.
func (c *Client) UpdateCheckRun(ctx context.Context, repo string, id int64, w CheckRunWrite) (*CheckRun, error) {
	var out restCheckRun
	path := repoPath(repo) + "/check-runs/" + strconv.FormatInt(id, 10)
	if err := c.do(ctx, repo, http.MethodPatch, path, w, &out); err != nil {
		return nil, err
	}
	r := out.run()
	return &r, nil
}
