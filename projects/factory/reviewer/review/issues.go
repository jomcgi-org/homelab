package review

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

// Itsaplan deliberately exposes reads only. The service holds this client's
// project-scoped key; neither the review agent nor its sandbox receives it.
type Itsaplan interface {
	GetProject(context.Context, string) (*itsaplan.Project, error)
	GetIssueByNumber(context.Context, string, int64) (*itsaplan.Issue, error)
	FindIssue(context.Context, string) (*itsaplan.Issue, error)
	IssueAuthor(context.Context, int64) (string, error)
	IssueTextAuthors(context.Context, *itsaplan.Issue) ([]string, error)
	LinkedComment(context.Context, int64, int64) (*itsaplan.FeedItem, error)
}

const (
	maxIssueText     = 16000
	maxDecisionText  = 4000
	maxDecisionLinks = 10
	issueReadTimeout = 30 * time.Second
)

type IssueEvidence struct {
	Key, Title, Description, Status, Author string
	Trusted, Clipped                        bool
}

type DecisionEvidence struct {
	URL, Author, Text, Reason string
	Exists, Trusted, Clipped  bool
}

type IssueContext struct {
	Issue     *IssueEvidence
	Decisions []DecisionEvidence
	Findings  []Finding
}

func issueDigest(in *IssueContext) string {
	data, _ := json.Marshal(in) // Only strings, booleans and slices.
	return fmt.Sprintf("%x", sha256.Sum256(data))
}

// Include the truncation marker in the byte budget (clip alone adds it).
func boundedIssueText(s string, n int) string {
	if len(s) > n {
		return clip(s, n-len("…"))
	}
	return s
}

// issueNumber accepts only a canonical key, never a path or a partial number.
func issueNumber(key, project string) int64 {
	suffix, ok := strings.CutPrefix(key, project+"-")
	if !ok || suffix == "" || suffix[0] == '0' {
		return 0
	}
	n, err := strconv.ParseInt(suffix, 10, 64)
	if err != nil || n <= 0 || strconv.FormatInt(n, 10) != suffix {
		return 0
	}
	return n
}

func firstRef(text, project string) string {
	re := regexp.MustCompile(`\bRefs[ \t]+(` + regexp.QuoteMeta(project) + `-[1-9][0-9]*)\b`)
	match := re.FindStringSubmatch(text)
	if len(match) == 2 && issueNumber(match[1], project) > 0 {
		return match[1]
	}
	return ""
}

func resolveIssue(ctx context.Context, gh GitHub, b *Binding, pr github.PullRequest) (string, error) {
	prefix := b.BranchPrefix
	if prefix == "" {
		prefix = "factory/"
	}
	if pr.Author == "jomcgi-factory[bot]" && strings.HasPrefix(pr.HeadRef, prefix) {
		key := strings.TrimPrefix(pr.HeadRef, prefix)
		if issueNumber(key, b.ItsaplanProject) == 0 {
			return "", errors.New("invalid factory issue branch")
		}
		return key, nil // PR text cannot override the connector's branch.
	}
	if key := firstRef(pr.Body, b.ItsaplanProject); key != "" {
		return key, nil
	}
	messages, err := gh.PullRequestCommitMessages(ctx, b.repo(), pr.Number, pr.HeadSHA)
	if err != nil {
		return "", err
	}
	for _, message := range messages {
		if key := firstRef(message, b.ItsaplanProject); key != "" {
			return key, nil
		}
	}
	return "", nil
}

// Only API account metadata can grant authority. In particular an agent with
// a trusted-looking name, or a body claiming to speak for Joe, grants none.
func issueAccount(p *itsaplan.Project, userID string, trusted []string) (string, bool) {
	for _, account := range p.Assignees {
		if userID != "" && account.UserID == userID {
			return account.Username, account.Kind == "member" && !strings.EqualFold(account.Username, "factoryadmin") && containsFold(trusted, account.Username)
		}
	}
	return "", false
}

var (
	decisionURL = regexp.MustCompile("https://factory-plan\\.jomcgi\\.dev/factory/issue/HOME-[^\\s<>\\[\\]()\"'`]+")
	decisionLink = regexp.MustCompile(`^https://factory-plan\.jomcgi\.dev/factory/issue/(HOME-[1-9][0-9]*)(?:#comment-([1-9][0-9]*))?$`)
)

type decisionRef struct {
	url, key string
	comment  int64
}

// decisionRefs scans both sides of the diff, including removed links. Missing or
// truncated GitHub patches cannot certify that all decision links were read.
func decisionRefs(b *Binding, files []github.PullRequestFile) ([]decisionRef, bool) {
	var refs []decisionRef
	seen := map[string]bool{}
	incomplete := false
	for _, file := range files {
		added, deleted := 0, 0
		for _, line := range strings.Split(file.Patch, "\n") {
			if strings.HasPrefix(line, "-") {
				deleted++
			}
			if strings.HasPrefix(line, "+") {
				added++
			}
			if !strings.HasPrefix(line, "+") && !strings.HasPrefix(line, "-") && !strings.HasPrefix(line, " ") {
				continue
			}
			for _, raw := range decisionURL.FindAllString(line[1:], -1) {
				raw = strings.TrimRight(raw, ".,;:")
				if seen[raw] {
					continue
				}
				seen[raw] = true
				if len(refs) >= maxDecisionLinks {
					incomplete = true
					continue
				}
				match := decisionLink.FindStringSubmatch(raw)
				if len(match) == 0 || issueNumber(match[1], b.ItsaplanProject) == 0 {
					incomplete = true
					continue
				}
				id, _ := strconv.ParseInt(match[2], 10, 64)
				if match[2] != "" && id <= 0 {
					incomplete = true
					continue
				}
				refs = append(refs, decisionRef{match[0], match[1], id})
			}
		}
		// Generated files are read against their sources under REVIEW.md.
		if !matchAny(b.GeneratedPaths, file.Path) && (added != file.Additions || deleted != file.Deletions) {
			incomplete = true
		}
	}
	return refs, incomplete
}

func (in *IssueContext) unverified(id, detail string) {
	in.Findings = append(in.Findings, Finding{
		ID: "itsaplan:unverified:"+id, Severity: SeverityMedium,
		Title: "Issue evidence is unverified", Detail: detail,
	})
}

// Current service failures cannot be waived by a model's claimed fix or
// justification, including on a resumed task or a later head.
func applyIssueFindings(report *Report, in *IssueContext) {
	if in == nil {
		return
	}
	for _, finding := range in.Findings {
		report.Findings = slices.DeleteFunc(report.Findings, func(f Finding) bool { return f.ID == finding.ID })
		report.Previous = slices.DeleteFunc(report.Previous, func(p Previous) bool { return p.ID == finding.ID })
		report.Findings = append(report.Findings, finding)
	}
}

// issueContext gathers one goal issue and at most ten references per snapshot.
// Failures carry only fixed reason strings, never response bodies.
// Even a model reporting no findings cannot turn missing evidence into a pass.
func (e *Engine) issueContext(ctx context.Context, b *Binding, pr github.PullRequest, files []github.PullRequestFile) *IssueContext {
	in := &IssueContext{}
	if b.ItsaplanProject == "" {
		return in
	}
	ctx, cancel := context.WithTimeout(ctx, issueReadTimeout)
	defer cancel()
	key, err := resolveIssue(ctx, e.gh, b, pr)
	if err != nil {
		in.unverified("issue", "The issue reference could not be resolved. Restore the branch or commit read and review this head again; no issue evidence was supplied.")
		return in
	}
	refs, incomplete := decisionRefs(b, files)
	if incomplete {
		in.unverified("bounds", "The diff's decision links could not all be checked within the patch or ten-link bound. Supply a complete smaller diff; unchecked links grant no authority.")
	}
	if key == "" && len(refs) == 0 {
		return in
	}
	if e.cfg.Itsaplan == nil {
		in.unverified("read", "The itsaplan reader is unavailable. Restore it and review this head again; no issue evidence was supplied.")
		return in
	}
	p, err := e.cfg.Itsaplan.GetProject(ctx, b.ItsaplanProject)
	if err != nil || p == nil {
		in.unverified("read", "The itsaplan project could not be read. Restore access and review this head again; no issue evidence was supplied.")
		return in
	}
	var goal *itsaplan.Issue
	if key != "" {
		goal, err = e.cfg.Itsaplan.GetIssueByNumber(ctx, b.ItsaplanProject, issueNumber(key, b.ItsaplanProject))
		switch {
		case err != nil || goal == nil || goal.ProjectID != p.Project.ID || goal.Identifier != key:
			in.unverified("issue", "The referenced issue could not be read in the bound project. Restore access and review this head again; no issue evidence was supplied.")
			goal = nil
		default:
			uid, authorErr := e.cfg.Itsaplan.IssueAuthor(ctx, goal.ID)
			if authorErr != nil {
				in.unverified("author", "The issue's creation event could not be read. Its author and authority remain unverified; no issue text was supplied.")
				break
			}
			author, trusted := issueAccount(p, uid, b.TrustedItsaplanAuthors)
			authors, textErr := e.cfg.Itsaplan.IssueTextAuthors(ctx, goal)
			if textErr != nil {
				in.unverified("author", "The issue's text history could not be read; no issue text was supplied. The service will retry.")
				break
			}
			trusted = trusted && len(authors) == 2
			for _, editor := range authors {
				_, allowed := issueAccount(p, editor, b.TrustedItsaplanAuthors)
				trusted = trusted && allowed
			}
			status := ""
			for _, column := range p.Columns {
				if column.ID == goal.ColumnID {
					status = column.Name
					break
				}
			}
			if status == "" {
				in.unverified("status", "The issue's workflow status could not be resolved. Restore the project read; no issue text was supplied.")
				break
			}
			in.Issue = &IssueEvidence{
				Key: key, Title: boundedIssueText(goal.Title, maxPromptTitle),
				Description: boundedIssueText(goal.Description, maxIssueText),
				Status: boundedIssueText(status, 100), Author: boundedIssueText(author, 100),
				Trusted: trusted, Clipped: len(goal.Description) > maxIssueText || len(goal.Title) > maxPromptTitle,
			}
			if in.Issue.Clipped {
				in.unverified("size", "The issue exceeds the fixed text bound. Shorten its stated goal and criteria before review; the omitted text supplies no evidence.")
			}
		}
	}
	for _, ref := range refs {
		d := DecisionEvidence{URL: ref.url, Reason: "unreadable or missing"}
		linked := goal
		if linked == nil || linked.Identifier != ref.key {
			linked, err = e.cfg.Itsaplan.FindIssue(ctx, ref.key)
		} else {
			err = nil
		}
		if err == nil && linked != nil && linked.ProjectID == p.Project.ID {
			var uid string
			if ref.comment > 0 {
				var comment *itsaplan.FeedItem
				comment, err = e.cfg.Itsaplan.LinkedComment(ctx, linked.ID, ref.comment)
				if err == nil && comment != nil && comment.IssueID == linked.ID && comment.ID == ref.comment && comment.Kind == "comment" {
					d.Exists, uid = true, comment.ActorUserID
					d.Text, d.Clipped = boundedIssueText(comment.Body, maxDecisionText), len(comment.Body) > maxDecisionText
					if !comment.OriginalComment() {
						uid = "" // The original author does not authenticate an edit.
					}
				}
			} else {
				d.Exists = true
				// A mutable title is not authenticated by a creation event. Only
				// the goal's separately verified text can carry issue authority.
				d.Text = boundedIssueText(linked.Title, maxPromptTitle)
				if in.Issue != nil && in.Issue.Key == ref.key && in.Issue.Trusted {
					uid, err = e.cfg.Itsaplan.IssueAuthor(ctx, linked.ID)
				}
			}
			if d.Exists && err == nil {
				d.Author, d.Trusted = issueAccount(p, uid, b.TrustedItsaplanAuthors)
				d.Author = boundedIssueText(d.Author, 100)
				d.Reason = "untrusted author"
				if d.Trusted {
					d.Reason = "verified authorship"
				}
				if d.Clipped {
					d.Reason = "text exceeds bound"
				}
			}
		}
		if err != nil {
			d.Text, d.Author, d.Trusted = "", "", false
		}
		if err != nil && !errors.Is(err, itsaplan.ErrNotFound) {
			in.unverified(ref.key+"-"+strconv.FormatInt(ref.comment, 10), "A linked work item could not be read. No evidence was supplied for it; the service will retry.")
		}
		in.Decisions = append(in.Decisions, d)
	}
	return in
}

func issuesPending(run *github.CheckRun) bool {
	if run == nil {
		return false
	}
	st, _ := LoadState(run.Text)
	return st.IssuesPending
}

// holdIssues revokes obsolete evidence before scheduling another review. Keep
// findings and human holds; neither an evidence edit nor recovery clears them.
func (e *Engine) holdIssues(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, st State) *github.CheckRun {
	st.IssuesPending = true
	o := Outcome{Conclusion: ConclusionFailure, Output: github.CheckRunOutput{
		Title: "Waiting for current issue evidence",
		Summary: "Issue evidence changed or could not be verified. This head cannot pass on its earlier evidence. The service will retry reads and run a fresh review on the same head when evidence is available; no push is required.",
		Text: encodeState(st),
	}, Comment: &CommentUpdate{
		Verdict: VerdictHold, Headline: "Waiting for current issue evidence", Gate: st.Gate,
		Note: "Issue evidence changed or could not be verified. The service will retry this head; no push is required.",
	}}
	if e.finish(ctx, b, pr, run, o, e.prLog(b, pr)) {
		cur := *run
		cur.Status, cur.Conclusion = "completed", o.Conclusion
		cur.Title, cur.Summary, cur.Text = o.Output.Title, o.Output.Summary, o.Output.Text
		return &cur
	}
	// A refused update must not leave the only durable check as a pass when
	// GitHub still accepts a new failing check on the same head.
	now := e.cfg.Now().UTC()
	cur, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
		Status: "completed", Conclusion: ConclusionFailure, StartedAt: &now, CompletedAt: &now, Output: &o.Output,
	})
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "create_check", err)
		return nil // Unknown approval never authorizes enqueue or a merge group.
	}
	e.mirror(ctx, b, pr, cur, o.Comment)
	return cur
}

// reconcileIssues runs before completed-check reconciliation and queue
// authorization on every poll, including after a process restart. Pending reads
// release review slots and recover on this head without retrying model work.
func (e *Engine) reconcileIssues(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (current *github.CheckRun, pending, ready bool) {
	if b.ItsaplanProject == "" {
		return run, false, false
	}
	st, ok := LoadState(run.Text)
	if !ok && run.Conclusion != ConclusionSuccess {
		// A failed task with no report keeps the existing model-attempt bound.
		// Evidence read failures use the durable pending state instead.
		return run, false, false
	}
	files, err := e.gh.PullRequestFiles(ctx, b.repo(), pr.Number)
	var fresh *IssueContext
	if err == nil {
		fresh = e.issueContext(ctx, b, pr, files)
	}
	ready = fresh != nil && len(fresh.Findings) == 0
	if ready && !st.IssuesPending && st.IssueDigest == issueDigest(fresh) {
		return run, false, false
	}
	if st.IssuesPending && run.Conclusion == ConclusionFailure {
		return run, true, ready
	}
	return e.holdIssues(ctx, b, pr, run, st), true, ready
}
