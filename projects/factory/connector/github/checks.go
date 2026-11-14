package github

import (
	"context"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// CheckRunFilter narrows a check run listing. Zero fields do not filter.
type CheckRunFilter struct {
	Name  string
	AppID int64
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

// ListCheckRuns lists the latest check run per name on a commit, the first
// 100, optionally only those with one name or from one GitHub App.
func (c *Client) ListCheckRuns(ctx context.Context, repo, sha string, f CheckRunFilter) ([]CheckRun, error) {
	q := url.Values{"filter": {"latest"}, "per_page": {"100"}}
	if f.Name != "" {
		q.Set("check_name", f.Name)
	}
	if f.AppID != 0 {
		q.Set("app_id", strconv.FormatInt(f.AppID, 10))
	}
	var out struct {
		CheckRuns []restCheckRun `json:"check_runs"`
	}
	path := repoPath(repo) + "/commits/" + url.PathEscape(sha) + "/check-runs?" + q.Encode()
	if err := c.do(ctx, repo, http.MethodGet, path, nil, &out); err != nil {
		return nil, err
	}
	runs := make([]CheckRun, 0, len(out.CheckRuns))
	for _, r := range out.CheckRuns {
		runs = append(runs, r.run())
	}
	return runs, nil
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
