package review

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

// maxCited bounds the pull requests a description may have the service read.
const maxCited = 8

// CitedPR is the service's evidence about one pull request the description
// cites. Every field is a fixed class, a number, a time, a commit ID or a
// login checked against GitHub's login syntax: nothing an author wrote.
type CitedPR struct {
	Number int
	// Kind is "pull", "missing" (an issue or nothing) or "unread".
	Kind           string
	State          string // OPEN, CLOSED, MERGED or "other"
	OnBase         bool   // targets the binding's base branch
	MergedAt       time.Time
	MergeCommitSHA string
	HeadSHA        string
	Queued         bool
	QueuedBy       string
	Author         string
	MergedBy       string
	// AutoMergeEnabledBy enabled auto-merge last, even after the merge;
	// AutoMergeBy holds a still-pending request.
	AutoMergeEnabledBy string
	AutoMergeBy        string
	HeadAuthor         string
	HeadSignature      string // GitHub's signature state, or "" when unsigned
	HeadByGitHub       bool
	Checks             []CitedCheck
}

// CitedCheck is a check the binding trusts, matched by name and creator.
type CitedCheck struct {
	Name   string // from the reviewer's own configuration
	Result string // a check conclusion or status state, "pending" or "missing"
}

var (
	// citedNumber matches "#123" not inside a word, a path or an HTML
	// entity, and github.com/<owner>/<repo>/pull/123 links.
	citedNumber = regexp.MustCompile(`(?:^|[^\w/&#])#([0-9]{1,7})\b`)
	loginSyntax = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$`)
	commitSHA   = regexp.MustCompile(`^[0-9a-f]{40}$`)
	enumSyntax  = regexp.MustCompile(`^[A-Za-z_]{1,32}$`)
)

// citedNumbers lists the PR numbers the description cites, in order, once
// each, without the PR's own number, at most maxCited.
func citedNumbers(repo string, self int, body string) []int {
	link := regexp.MustCompile(`https://github\.com/` + regexp.QuoteMeta(repo) + `/(?:pull|issues)/([0-9]{1,7})\b`)
	type hit struct{ at, n int }
	var hits []hit
	for _, re := range []*regexp.Regexp{citedNumber, link} {
		for _, m := range re.FindAllStringSubmatchIndex(body, -1) {
			if n, err := strconv.Atoi(body[m[2]:m[3]]); err == nil && n > 0 {
				hits = append(hits, hit{m[2], n})
			}
		}
	}
	// Order by position so the first citations win when there are more.
	for i := 1; i < len(hits); i++ {
		for j := i; j > 0 && hits[j].at < hits[j-1].at; j-- {
			hits[j], hits[j-1] = hits[j-1], hits[j]
		}
	}
	var out []int
	seen := map[int]bool{self: true}
	for _, h := range hits {
		if !seen[h.n] && len(out) < maxCited {
			seen[h.n] = true
			out = append(out, h.n)
		}
	}
	return out
}

// citedEvidence reads the pull requests the description cites. Their titles
// and text never reach the prompt; logins pass only in GitHub's syntax.
// A read that fails marks that PR unread rather than holding the review:
// the agent then reports what it cites as unverified.
func (e *Engine) citedEvidence(ctx context.Context, b *Binding, pr github.PullRequest, l *slog.Logger) []CitedPR {
	var out []CitedPR
	for _, n := range citedNumbers(b.repo(), pr.Number, pr.Body) {
		p, err := e.gh.CitedPullRequest(ctx, b.repo(), n)
		switch {
		case errors.Is(err, github.ErrNotFound):
			out = append(out, CitedPR{Number: n, Kind: "missing"})
			continue
		case err != nil:
			e.ghError(ctx, l, b, "cited", err)
			out = append(out, CitedPR{Number: n, Kind: "unread"})
			continue
		}
		c := CitedPR{
			Number: n, Kind: "pull", State: enumOr(p.State, "other"), OnBase: p.BaseRef == b.BaseBranch,
			MergedAt: p.MergedAt, MergeCommitSHA: shaOr(p.MergeCommitSHA), HeadSHA: shaOr(p.HeadSHA), Queued: p.Queued,
			QueuedBy: loginOr(p.QueuedBy), Author: loginOr(p.Author), MergedBy: loginOr(p.MergedBy),
			AutoMergeEnabledBy: loginOr(p.AutoMergeEnabledBy), AutoMergeBy: loginOr(p.AutoMergeBy),
			HeadAuthor: loginOr(p.HeadCommitAuthor), HeadSignature: enumOr(p.HeadSignature, ""), HeadByGitHub: p.HeadSignedByGitHub,
		}
		if c.HeadSHA != "" {
			if c.Checks, err = e.citedChecks(ctx, b, c.HeadSHA); err != nil {
				e.ghError(ctx, l, b, "cited_checks", err)
				c.Checks = []CitedCheck{{Name: "required checks", Result: "unread"}}
			}
		}
		out = append(out, c)
	}
	return out
}

// citedChecks reports the binding's required checks and the reviewer's own
// check on sha, each matched by name and its creating App or status creator.
func (e *Engine) citedChecks(ctx context.Context, b *Binding, sha string) ([]CitedCheck, error) {
	want := append([]Check{{Name: e.cfg.CheckName, AppID: e.cfg.AppID}}, b.RequiredChecks...)
	var statuses []github.Status
	fetched := false
	var out []CitedCheck
	for _, c := range want {
		result := "missing"
		if c.AppID != 0 {
			runs, err := e.gh.ListCheckRuns(ctx, b.repo(), sha, github.CheckRunFilter{Name: c.Name, AppID: c.AppID})
			if err != nil {
				return nil, err
			}
			var latest *github.CheckRun
			for i := range runs {
				r := &runs[i]
				if r.Name == c.Name && r.AppID == c.AppID && r.HeadSHA == sha && (latest == nil || r.ID > latest.ID) {
					latest = r
				}
			}
			switch {
			case latest == nil:
			case latest.Status != "completed":
				result = "pending"
			default:
				result = enumOr(latest.Conclusion, "other")
			}
		} else {
			if !fetched {
				var err error
				if statuses, err = e.gh.Statuses(ctx, b.repo(), sha); err != nil {
					return nil, err
				}
				fetched = true
			}
			// GitHub lists a commit's statuses newest first.
			for _, s := range statuses {
				if s.Context == c.Name && strings.EqualFold(s.Creator, c.StatusCreator) && s.SHA == sha {
					result = enumOr(s.State, "other")
					break
				}
			}
		}
		out = append(out, CitedCheck{Name: c.Name, Result: strings.ToLower(result)})
	}
	return out, nil
}

func loginOr(s string) string {
	if s == "" {
		return ""
	}
	if loginSyntax.MatchString(s) {
		return s
	}
	return "(unrecognized login)"
}

func shaOr(s string) string {
	if commitSHA.MatchString(s) {
		return s
	}
	return ""
}

func enumOr(s, other string) string {
	if s == "" {
		return ""
	}
	if enumSyntax.MatchString(s) {
		return s
	}
	return other
}

// writeCited writes the evidence lines, one pull request per paragraph.
func writeCited(b *strings.Builder, base string, cited []CitedPR) {
	for _, c := range cited {
		switch c.Kind {
		case "missing":
			fmt.Fprintf(b, "#%d: not a pull request in this repository (an issue, or nothing).\n", c.Number)
			continue
		case "unread":
			fmt.Fprintf(b, "#%d: the reviewer service could not read it.\n", c.Number)
			continue
		}
		target := base
		if !c.OnBase {
			target = "a branch other than " + base
		}
		fmt.Fprintf(b, "#%d: pull request into %s, state %s", c.Number, target, orNone(c.State))
		if !c.MergedAt.IsZero() {
			fmt.Fprintf(b, ", merged at %s", c.MergedAt.UTC().Format(time.RFC3339))
		}
		if c.MergeCommitSHA != "" {
			fmt.Fprintf(b, " as %s", c.MergeCommitSHA)
		}
		if c.Queued {
			fmt.Fprintf(b, "; its timeline shows it entered the merge queue, last added by %s", orNone(c.QueuedBy))
		} else {
			b.WriteString("; its timeline shows no merge queue entry")
		}
		fmt.Fprintf(b, ".\n- author %s; merged by %s; auto-merge last enabled by %s; pending auto-merge request by %s\n",
			orNone(c.Author), orNone(c.MergedBy), orNone(c.AutoMergeEnabledBy), orNone(c.AutoMergeBy))
		if c.HeadSHA != "" {
			fmt.Fprintf(b, "- head %s: commit author account %s; signature %s", c.HeadSHA, orNone(c.HeadAuthor), orNone(c.HeadSignature))
			if c.HeadByGitHub {
				b.WriteString(" (signed by GitHub)")
			}
			b.WriteString("\n")
		}
		for _, ch := range c.Checks {
			fmt.Fprintf(b, "- check %q on the head: %s\n", ch.Name, ch.Result)
		}
	}
}

func orNone(s string) string {
	if s == "" {
		return "none"
	}
	return s
}
