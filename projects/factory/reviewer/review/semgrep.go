package review

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

const (
	semgrepTimeout     = 20 * time.Second
	semgrepMaxFindings = 100
	semgrepMaxResponse = 1 << 20
	semgrepMaxEvidence = 64000
	semgrepUnverified  = "semgrep:unverified"
)

// SemgrepClient queries the same deployment findings endpoint as Semgrep's
// official client (cli/src/semgrep/mcp/server.py, semgrep_findings). This is
// a read query, even though that API uses POST. It runs only in the service.
type SemgrepClient struct {
	APIURL    string
	TokenFile string
	ScanCheck github.CheckRunFilter
	HTTP      *http.Client
}

// SemgrepFinding holds bounded service-side evidence for validation and the
// restart digest. It must not be marshaled into a prompt: semgrepToolOutput
// drops provider text and maps the permitted fields to fixed classes.
type SemgrepFinding struct {
	ID       int64  `json:"id"`
	Product  string `json:"product"`
	Path     string `json:"path"`
	Line     int    `json:"line"`
	Rule     string `json:"rule"`
	Message  string `json:"message"`
	Severity string `json:"severity"`
	Triage   string `json:"triage"`
	URL      string `json:"url"`
}

// SemgrepEvidence belongs to one PR head. Unavailable is service-owned text;
// nonempty means a mandatory medium finding, regardless of the agent's reply.
type SemgrepEvidence struct {
	Repository  string           `json:"repository"`
	Ref         string           `json:"ref"`
	Head        string           `json:"head"`
	Findings    []SemgrepFinding `json:"findings"`
	Unavailable string           `json:"unavailable,omitempty"`
}

func (s *SemgrepEvidence) digest() string {
	if s == nil {
		return ""
	}
	b, _ := json.Marshal(s)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (s *SemgrepClient) fetch(ctx context.Context, gh GitHub, repo, ref, head string) *SemgrepEvidence {
	ctx, cancel := context.WithTimeout(ctx, semgrepTimeout)
	defer cancel()
	out := &SemgrepEvidence{Repository: repo, Ref: ref, Head: head, Findings: []SemgrepFinding{}}
	fail := func(reason string) *SemgrepEvidence {
		out.Unavailable = reason
		return out
	}
	if ref == "" {
		return fail("the PR head ref is missing")
	}
	token, err := os.ReadFile(s.TokenFile)
	if err != nil || len(bytes.TrimSpace(token)) == 0 {
		return fail("the Semgrep API_KEY token file is missing, unreadable or empty")
	}
	// A completed, successful scan check on this SHA distinguishes a clean
	// scan from an unscanned ref. Check both name and App even if GitHub
	// ignores the request filter. A failure can mean findings rather than an
	// execution error; below, it also needs findings with exact-head scan metadata.
	checks, err := gh.ListCheckRuns(ctx, repo, head, s.ScanCheck)
	var latest *github.CheckRun
	if err == nil && s.ScanCheck.AppID > 0 && s.ScanCheck.Name != "" {
		for _, c := range checks {
			if c.Name == s.ScanCheck.Name && c.AppID == s.ScanCheck.AppID && c.HeadSHA == head && (latest == nil || c.ID > latest.ID) {
				latest = &c
			}
		}
	}
	if latest == nil || latest.Status != "completed" || latest.Conclusion != "success" {
		out.Unavailable = "no successful Semgrep scan check from the configured App for this head yet (or the check could not be read)"
	}
	allFindingsAtHead := true
	u, err := url.Parse(s.APIURL)
	if err != nil {
		return fail("the Semgrep API URL is invalid")
	}
	parts := strings.Split(strings.Trim(u.Path, "/"), "/")
	if len(parts) != 6 {
		return fail("the Semgrep API URL does not name a deployment findings endpoint")
	}
	for _, product := range []string{"ISSUE_TYPE_SAST", "ISSUE_TYPE_SCA"} {
		// Request one extra so reaching the bound never looks like a complete
		// result. There is no unbounded pagination or automatic retry on 5xx.
		limit := semgrepMaxFindings - len(out.Findings) + 1
		body, _ := json.Marshal(map[string]any{
			"deploymentId": parts[3], "issueType": product, "limit": limit,
			"filter": map[string]any{
				"repositoryNames": []string{repo}, "refs": []string{ref},
				"status":     []string{"ISSUE_TAB_OPEN", "ISSUE_TAB_REVIEWING", "ISSUE_TAB_FIXING", "ISSUE_TAB_IGNORED"},
				"severities": []string{}, "confidences": []string{}, "aiVerdicts": []string{},
			},
		})
		data, reason := s.query(ctx, strings.TrimSpace(string(token)), body)
		if reason != "" {
			return fail(reason)
		}
		var result struct {
			Issues *[]struct {
				Issue struct {
					ID         json.Number `json:"id"`
					Ref        string      `json:"ref"`
					Repository struct {
						Name string `json:"name"`
					} `json:"repository"`
					FilePath     string `json:"filePath"`
					Line         int    `json:"line"`
					RulePath     string `json:"rulePath"`
					Message      string `json:"message"`
					Severity     string `json:"severity"`
					Triage       string `json:"triageState"`
					URL          string `json:"lineOfCodeUrl"`
					LastSeenScan struct {
						Meta struct {
							Commit string `json:"commit"`
						} `json:"meta"`
					} `json:"lastSeenScan"`
				} `json:"issue"`
			} `json:"issues"`
		}
		if json.Unmarshal(data, &result) != nil || result.Issues == nil {
			return fail("the Semgrep API returned an invalid findings response")
		}
		for _, row := range *result.Issues {
			f := row.Issue
			id, idErr := f.ID.Int64()
			if idErr != nil || id <= 0 || f.Repository.Name != repo || f.Ref != ref || f.RulePath == "" || f.FilePath == "" {
				return fail("the Semgrep API returned malformed findings or findings outside the requested repository/ref")
			}
			if f.LastSeenScan.Meta.Commit != "" && f.LastSeenScan.Meta.Commit != head {
				return fail("the Semgrep findings still describe a scan of another commit")
			}
			allFindingsAtHead = allFindingsAtHead && f.LastSeenScan.Meta.Commit == head
			if len(out.Findings) == semgrepMaxFindings {
				return fail("the Semgrep findings exceed the 100-finding review limit; narrow or split the PR and review again")
			}
			if len(f.Message) > 4000 || len(f.FilePath) > 1000 || len(f.RulePath) > 1000 || len(f.URL) > 2000 || len(f.Severity) > 100 || len(f.Triage) > 100 {
				return fail("a Semgrep finding exceeds the review evidence size limit")
			}
			out.Findings = append(out.Findings, SemgrepFinding{
				ID: id, Product: product, Path: f.FilePath,
				Line: f.Line, Rule: f.RulePath, Message: f.Message, Severity: f.Severity, Triage: f.Triage, URL: f.URL,
			})
			if encoded, _ := json.Marshal(out); len(encoded) > semgrepMaxEvidence {
				out.Findings = out.Findings[:len(out.Findings)-1]
				return fail("the Semgrep findings exceed the 64000-byte prompt evidence limit")
			}
		}
	}
	// Semgrep's finding verdict does not set REVIEW.md severity. A failed
	// completed check with actual findings from this SHA proves a scan ran;
	// an empty response, missing metadata or an API error cannot do so.
	if latest != nil && latest.Status == "completed" && latest.Conclusion == "failure" && len(out.Findings) > 0 && allFindingsAtHead {
		out.Unavailable = ""
	}
	// Stable evidence digests across API ordering changes on a restart.
	sort.Slice(out.Findings, func(i, j int) bool {
		if out.Findings[i].Product != out.Findings[j].Product {
			return out.Findings[i].Product < out.Findings[j].Product
		}
		return out.Findings[i].ID < out.Findings[j].ID
	})
	return out
}

// query returns only fixed error classes. Error bodies and transport errors
// may contain credentials or provider text and must never reach logs/prompts.
func (s *SemgrepClient) query(ctx context.Context, token string, body []byte) ([]byte, string) {
	client := http.Client{}
	if s.HTTP != nil {
		client = *s.HTTP
	}
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	for attempt := 0; attempt < 3; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.APIURL, bytes.NewReader(body))
		if err != nil {
			return nil, "the Semgrep API request could not be constructed"
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Accept", "application/json")
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return nil, "the Semgrep API request failed or exceeded the 20s time limit"
		}
		if resp.StatusCode == http.StatusTooManyRequests {
			retryAfter := resp.Header.Get("Retry-After")
			resp.Body.Close()
			if attempt == 2 {
				return nil, "the Semgrep API remained rate limited (HTTP 429)"
			}
			delay := time.Second << attempt
			if seconds, err := strconv.Atoi(retryAfter); err == nil && seconds > 0 {
				// Clamp before conversion to avoid overflowing time.Duration.
				delay = max(delay, time.Duration(min(seconds, 21))*time.Second)
			} else if when, err := http.ParseTime(retryAfter); err == nil {
				delay = max(delay, time.Until(when))
			}
			if deadline, ok := ctx.Deadline(); ok && delay >= time.Until(deadline) {
				return nil, "the Semgrep API rate limit exceeds the fetch time budget (HTTP 429)"
			}
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, "the Semgrep API fetch was cancelled or timed out during rate-limit backoff"
			case <-timer.C:
			}
			continue
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Sprintf("the Semgrep API returned HTTP %d (401/403 requires a Web API token with read-findings access)", resp.StatusCode)
		}
		data, err := io.ReadAll(io.LimitReader(resp.Body, semgrepMaxResponse+1))
		resp.Body.Close()
		if err != nil {
			return nil, "the Semgrep API response could not be read"
		}
		if len(data) > semgrepMaxResponse {
			return nil, "the Semgrep API response exceeds the 1 MiB limit"
		}
		return data, ""
	}
	return nil, "the Semgrep API retry budget was exhausted"
}

// enforceSemgrep runs after assessment so an agent cannot waive this service
// finding using a previous finding's justified/fixed status or low severity.
func enforceSemgrep(a *Assessment, evidence *SemgrepEvidence) {
	if evidence == nil || evidence.Unavailable == "" {
		return
	}
	a.Blocking = append(a.Blocking, Blocked{Finding: Finding{
		ID: semgrepUnverified, Severity: SeverityMedium,
		Title:  "Semgrep evidence for this head is unverified",
		Detail: evidence.Unavailable + ". Obtain complete Semgrep findings after a successful scan of this head, then rerun the review.",
	}})
}
