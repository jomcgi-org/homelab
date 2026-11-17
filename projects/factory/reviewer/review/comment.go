package review

// The reviewer keeps one conversation comment on each PR it reviews, edited
// in place on every head: the verdict, the agent's summary and the findings
// with their severities. The check run stays the gate. The comment shows the
// same assessment to people reading the PR, so writing it is best effort.

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"html"
	"io"
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

// Comments writes the reviewer's PR comment through the broker, which holds
// the reviewer App's comment token (issues and pull requests write) and
// finds, creates and edits only conversation comments with it. Its
// IssueComments returns only the oldest comment the bot login wrote that
// starts with CommentMarker, which the broker finds in one request. An App without those permissions loses
// only the comment.
type Comments interface {
	IssueComments(ctx context.Context, repo string, number int) ([]github.Comment, bool, error)
	CreateIssueComment(ctx context.Context, repo string, number int, body string) (*github.Comment, error)
	UpdateIssueComment(ctx context.Context, repo string, id int64, body string) (*github.Comment, error)
}

// Verdict is what the comment says of the head it describes.
type Verdict string

const (
	VerdictReviewing Verdict = "reviewing"
	VerdictApproved  Verdict = "approved"
	VerdictChanges   Verdict = "changes"
	VerdictHuman     Verdict = "human"
	VerdictHold      Verdict = "hold"
	VerdictFailed    Verdict = "failed"
)

var verdictHeading = map[Verdict]string{
	VerdictReviewing: "⏳ Reviewing",
	VerdictApproved:  "✅ Approved",
	VerdictChanges:   "❌ Changes requested",
	VerdictHuman:     "🙋 Waiting for a human approval",
	VerdictHold:      "⏸️ On hold",
	VerdictFailed:    "⚠️ Review incomplete",
}

var severityLabel = map[string]string{
	SeverityCritical: "🔴 Critical",
	SeverityHigh:     "🟠 High",
	SeverityMedium:   "🟡 Medium",
	SeverityLow:      "⚪ Low",
}

// CommentUpdate is what a review check write shows in the PR comment.
type CommentUpdate struct {
	// body replays a rendered snapshot recovered from a completed check.
	body    string
	Verdict Verdict
	// Headline is one short line under the verdict.
	Headline string
	// Note is a sentence the verdict needs: why a review failed or waits.
	Note string
	// Gate lists why the head needs a human approval, whether or not one
	// cleared it; the verdict says which.
	Gate *Gate
	// Findings replaces the findings section when set. Otherwise the
	// comment keeps its section, labelled with the head it came from.
	Findings *string
}

// CommentMarker starts the reviewer's PR comment. The broker finds the
// comment by it and the bot login.
const CommentMarker = commentMarker

// Comment markers. Every one starts with markerStart, which is stripped
// from anything the agent or the PR wrote, so only the service writes them.
const (
	commentMarker = markerStart + "comment v1 -->"
	findingsStart = markerStart + "findings %s -->"
	findingsEnd   = markerStart + "findings-end -->"
)

var findingsRE = regexp.MustCompile(`(?s)<!-- factory-review:findings ([0-9a-f]*) -->\n(.*?)\n<!-- factory-review:findings-end -->`)

var commentSnapshotRE = regexp.MustCompile(`<!-- factory-review:comment-snapshot v1 ([A-Za-z0-9+/=]*) -->`)

// recordComment stores the desired display with the check, before optional
// comment I/O starts. Gate-only checks carry the previous findings even when
// their comment has never been written. Check state keeps its full budget;
// an oversized display is shortened with a link to the check's full output.
func recordComment(text, previous, head string, u *CommentUpdate) string {
	if u == nil {
		return text
	}
	findingsHead, findings := parseFindings(recordedComment(previous))
	if u.Findings != nil {
		findingsHead, findings = head, *u.Findings
	}
	// The check URL is assigned on creation. Add its footer when replaying.
	body := renderCommentBody(head, u, findings, findingsHead)
	text = commentSnapshotRE.ReplaceAllString(text, "")
	for {
		var buf bytes.Buffer
		w := gzip.NewWriter(&buf)
		_, _ = io.WriteString(w, body)
		_ = w.Close()
		encoded := base64.StdEncoding.EncodeToString(buf.Bytes())
		snapshot := markerStart + "comment-snapshot v1 " + encoded + " -->"
		if len(text)+len(snapshot)+2 <= maxSummary {
			return text + "\n\n" + snapshot
		}
		// Keep complete findings markers when reducing the display, so a
		// later gate check can still carry the shortened findings forward.
		if len(findings) > 0 {
			findings = clip(findings, len(findings)/2)
			if len(findings) < 100 {
				findings = ""
			}
			body = renderCommentBody(head, u, findings+"\n\n_More detail is on the check run._", findingsHead)
		} else if len(body) > 100 {
			body = clip(body, len(body)/2) + "\n\n_More detail is on the check run._\n\n"
		} else {
			// Unexpected oversized state must not prevent the check write.
			return text
		}
	}
}

func recordedComment(text string) string {
	m := commentSnapshotRE.FindStringSubmatch(text)
	if m == nil {
		return ""
	}
	data, err := base64.StdEncoding.DecodeString(m[1])
	if err != nil {
		return ""
	}
	r, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return ""
	}
	defer r.Close()
	body, err := io.ReadAll(io.LimitReader(r, maxComment+1))
	if err != nil || len(body) > maxComment || !bytes.HasPrefix(body, []byte(commentMarker+"\n")) {
		return ""
	}
	return string(body)
}

// replayComment runs on the poller after gate reconciliation. Queueing is
// idempotent per check in this process; a failed attempt can retry next poll.
func (e *Engine) replayComment(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) {
	if run == nil || run.Status != "completed" || run.Conclusion == ConclusionCancelled {
		return
	}
	e.commentQueueMu.Lock()
	id, seen := e.commentReplays[prKey{b.Name, pr.Number}]
	queued := seen && id == run.ID
	e.commentQueueMu.Unlock()
	if queued {
		return
	}
	if body := recordedComment(run.Text); body != "" {
		e.queueMirror(ctx, b, pr, run, &CommentUpdate{body: body})
	}
}

// queueCompletedComment uses the same snapshot for live writes and retries.
// This also carries findings when an earlier queued write failed.
func (e *Engine) queueCompletedComment(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, text string, u *CommentUpdate) {
	if u == nil {
		return
	}
	update := *u
	update.body = recordedComment(text)
	e.queueMirror(ctx, b, pr, run, &update)
}

// Comment limits. GitHub takes 65536 characters in a comment body.
const (
	maxComment      = 60000
	maxFindings     = 50000
	maxCommentNote  = 4000
	maxSince        = 10000
	maxSummaryChars = maxAgentSummary
	commentTimeout  = 30 * time.Second
)

const commentLimitNote = "This PR has more than 10,000 comments, so its findings are only in this check."

// reviewComment is a completed review's comment: the verdict from the
// assessment, the human gate, then the findings section.
func reviewComment(a Assessment, r *Report, b *Binding, gate *Gate) *CommentUpdate {
	u := &CommentUpdate{Verdict: VerdictApproved, Gate: gate}
	gated := slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return isGate(b.Finding) })
	switch {
	case len(a.Blocking) == 0 && gate != nil && gate.Authorship != "":
		u.Headline = "Human gate cleared by authorship"
	case len(a.Blocking) == 0 && a.Approver != "":
		u.Headline = "Human gate cleared by " + a.Approver
	case len(a.Blocking) == 0 && len(a.Optional)+len(a.Deferred)+a.DroppedLow+len(a.Accepted) == 0:
		u.Headline = "No findings"
	case len(a.Blocking) == 0:
		u.Headline = "No blocking findings"
	case !slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return !isGate(b.Finding) }):
		u.Verdict, u.Headline = VerdictHuman, needsApproval(b)
	default:
		u.Verdict, u.Headline = VerdictChanges, blockingCounts(a.Blocking)
		if gated {
			u.Headline += " · needs a human approval"
		}
	}
	if n := len(a.Optional) + len(a.Deferred) + a.DroppedLow; n > 0 {
		u.Headline += fmt.Sprintf(" · %d optional", n)
	}
	if gated && gate != nil && gate.ApprovalNonce != "" {
		u.Note = acknowledgeNote(gate)
	}
	if gate != nil && gate.Authorship != "" {
		u.Note = gate.Authorship
	}
	body := findingsSection(a, r)
	u.Findings = &body
	return u
}

func needsApproval(b *Binding) string {
	return "Needs approval from " + strings.Join(b.HumanApprovers, " or ") + " on this head"
}

// acknowledgeNote is the line an approval that predates the hold must carry.
func acknowledgeNote(g *Gate) string {
	return "If your approval predates this check, add this line to it: `" + approvalAcknowledgement(g.ApprovalNonce) + "`"
}

// blockingCounts counts the blocking findings a revision can fix: the
// human gate is left out, since only an approval clears it.
func blockingCounts(bs []Blocked) string {
	counts, n := map[string]int{}, 0
	for _, b := range bs {
		if !isGate(b.Finding) {
			counts[b.Severity]++
			n++
		}
	}
	var parts []string
	for _, s := range Severities {
		if counts[s] > 0 {
			parts = append(parts, fmt.Sprintf("%d %s", counts[s], s))
		}
	}
	return fmt.Sprintf("%d blocking (%s)", n, strings.Join(parts, ", "))
}

// gateItems are the reasons a head needs a human approval, one per line.
func gateItems(g *Gate) []string {
	if g == nil {
		return nil
	}
	var out []string
	for _, p := range g.Paths {
		out = append(out, "touches `"+codeText(p)+"`")
	}
	for _, l := range g.Labels {
		out = append(out, "labelled `"+codeText(l)+"`")
	}
	if g.SharedHead {
		out = append(out, "shares its commit with another ready PR")
	}
	if g.RecoveryEpoch != "" {
		out = append(out, "context to confirm after a reviewer restart")
	}
	for _, f := range g.Findings {
		out = append(out, mdInline(f.Title, maxTitle, false)+" (`"+codeText(f.ID)+"`)")
	}
	return out
}

// row is one finding in the comment's table, with what its details add.
type row struct {
	f   Finding
	tag string
	// note is what the service or the agent says about a previous finding.
	note string
}

// findingsSection renders the agent's summary as a quote, a table of this
// head's findings (blocking first, then optional and deferred), a
// collapsed block of detail per finding, and what changed since the
// previous review. Past maxFindings it shortens the details, then leaves
// them out, then rows. Human-gate findings are not in it: the comment lists
// them with the gate, which an approval changes without a new review.
func findingsSection(a Assessment, r *Report) string {
	var rows []row
	for _, b := range a.Blocking {
		if isGate(b.Finding) {
			continue
		}
		rw := row{f: b.Finding}
		switch {
		case b.Carried:
			rw.tag, rw.note = "carried over", "Previous review: "+b.Reason+"."
		case b.Repeated:
			rw.tag = "still open"
			if b.Reason != "" {
				rw.note = "Previous review: " + b.Reason + "."
			}
		}
		if b.Note != "" {
			rw.note = strings.TrimSpace(rw.note + " Note: " + b.Note)
		}
		rows = append(rows, rw)
	}
	for _, f := range a.Optional {
		rows = append(rows, row{f: f})
	}
	for _, f := range a.Deferred {
		rw := row{f: f, tag: "follow-up"}
		if f.Lowered != "" {
			rw.note = loweredNote(f)
		}
		rows = append(rows, rw)
	}

	var head string
	if r != nil {
		if sum := mdPara(r.Summary, maxSummaryChars); sum != "" {
			head = "> " + strings.ReplaceAll(sum, "\n\n", "\n>\n> ") + "\n\n"
		}
	}
	tail := clip(sinceSection(a), maxSince)
	budget := maxFindings - len(head) - len(tail)
	for omitted := 0; ; omitted++ {
		shown := rows[:len(rows)-omitted]
		for _, limit := range []int{maxDetail, 500, 0} {
			body := findingsTable(shown, omitted, a.DroppedLow) + findingDetails(shown, limit)
			if len(body) <= budget || (limit == 0 && len(shown) == 0) {
				return strings.TrimRight(head+body+tail, "\n")
			}
		}
	}
}

func findingsTable(rows []row, omitted, dropped int) string {
	if len(rows)+omitted+dropped == 0 {
		return ""
	}
	var s strings.Builder
	if len(rows) > 0 {
		s.WriteString("| Severity | Finding | Location |\n| :-- | :-- | :-- |\n")
		for _, rw := range rows {
			title := mdInline(rw.f.Title, maxTitle, true)
			if rw.tag != "" {
				title += " · _" + rw.tag + "_"
			}
			fmt.Fprintf(&s, "| %s | %s | %s |\n", severityCell(rw.f.Severity), title, location(rw.f, true))
		}
		s.WriteString("\n")
	}
	if omitted > 0 {
		fmt.Fprintf(&s, "_%d more on the check run._\n\n", omitted)
	}
	if dropped > 0 {
		fmt.Fprintf(&s, "_%d more low dropped (at most %d listed)._\n\n", dropped, MaxLow)
	}
	return s.String()
}

func severityCell(sev string) string {
	label := severityLabel[sev]
	if sev == SeverityLow {
		return label
	}
	emoji, name, _ := strings.Cut(label, " ")
	return emoji + " **" + name + "**"
}

// location is a finding's path and line as a code span, or a dash.
func location(f Finding, table bool) string {
	p := cleanPath(f.Path)
	if p == "" {
		return "—"
	}
	if f.Line > 0 {
		p = fmt.Sprintf("%s:%d", p, f.Line)
	}
	if table {
		p = strings.ReplaceAll(p, "|", `\|`)
	}
	return "`" + p + "`"
}

// findingDetails renders a collapsed block per finding that has a detail or
// a note, its detail cut to limit characters; 0 renders none.
func findingDetails(rows []row, limit int) string {
	if limit == 0 {
		return ""
	}
	var s strings.Builder
	for _, rw := range rows {
		if rw.f.Detail == "" && rw.note == "" {
			continue
		}
		fmt.Fprintf(&s, "<details>\n<summary>%s · %s</summary>\n\n", severityLabel[rw.f.Severity], htmlInline(rw.f.Title, maxTitle))
		if d := mdPara(rw.f.Detail, min(limit, maxDetail)); d != "" {
			s.WriteString(d + "\n\n")
		}
		if rw.note != "" {
			s.WriteString("_" + mdInline(rw.note, min(limit, maxMessage), false) + "_\n\n")
		}
		fmt.Fprintf(&s, "<sub><code>%s</code></sub>\n</details>\n\n", htmlEscape(oneLine(rw.f.ID, maxTitle)))
	}
	return s.String()
}

// sinceSection lists what this review cleared: previous findings fixed,
// justified or withdrawn.
func sinceSection(a Assessment) string {
	if len(a.Fixed)+len(a.Accepted)+len(a.Withdrawn) == 0 {
		return ""
	}
	var s strings.Builder
	item := func(mark, what string, f Finding, note string) {
		fmt.Fprintf(&s, "- %s **%s** · %s", mark, what, mdInline(f.Title, maxTitle, false))
		if note != "" {
			s.WriteString(": _" + mdInline(note, maxMessage, false) + "_")
		}
		s.WriteString("\n")
	}
	s.WriteString("#### Since the previous review\n\n")
	for _, f := range a.Fixed {
		item("✅", "Fixed", f, "")
	}
	for _, b := range a.Accepted {
		item("☑️", "Justified", b.Finding, b.Note)
	}
	for _, b := range a.Withdrawn {
		item("↩️", "Withdrawn", b.Finding, b.Note)
	}
	s.WriteString("\n")
	return s.String()
}

// renderComment writes the whole comment for head: the verdict, a headline
// and note, the human gate, the findings section (collapsed when it came
// from another head) and a footer naming the commit and check.
func renderComment(head string, u *CommentUpdate, findings, findingsHead, checkName, checkURL string) string {
	return clip(renderCommentBody(head, u, findings, findingsHead)+commentFooter(head, checkName, checkURL), maxComment)
}

func renderCommentBody(head string, u *CommentUpdate, findings, findingsHead string) string {
	var s strings.Builder
	s.WriteString(commentMarker + "\n")
	fmt.Fprintf(&s, "### %s\n\n", verdictHeading[u.Verdict])
	if h := mdInline(u.Headline, maxTitle, false); h != "" {
		s.WriteString("**" + h + "**\n\n")
	}
	if n := mdPara(u.Note, maxCommentNote); n != "" {
		s.WriteString(n + "\n\n")
	}
	if items := gateItems(u.Gate); len(items) > 0 {
		s.WriteString("#### 🙋 Human gate\n\n")
		for _, it := range items {
			s.WriteString("- " + it + "\n")
		}
		s.WriteString("\n")
	}
	s.WriteString(renderCommentFindings(head, findings, findingsHead))
	return clip(s.String(), maxComment-1000)
}

func renderCommentFindings(head, findings, findingsHead string) string {
	var s strings.Builder
	// An empty section with a head explicitly clears earlier findings.
	if findingsHead != "" {
		earlier := findingsHead != head
		if earlier {
			fmt.Fprintf(&s, "<details>\n<summary>Previous review · <code>%s</code></summary>\n\n", short(findingsHead))
		}
		s.WriteString(fmt.Sprintf(findingsStart, findingsHead) + "\n" + clip(findings, maxFindings) + "\n" + findingsEnd + "\n\n")
		if earlier {
			s.WriteString("</details>\n\n")
		}
	}
	return s.String()
}

func commentFooter(head, checkName, checkURL string) string {
	var s strings.Builder
	fmt.Fprintf(&s, "<sub><code>%s</code>", short(head))
	if checkURL != "" {
		fmt.Fprintf(&s, " · <a href=\"%s\">%s</a>", htmlEscape(checkURL), htmlEscape(checkName))
	}
	s.WriteString("</sub>\n")
	return s.String()
}

// parseFindings reads the findings section of a comment the reviewer wrote.
func parseFindings(body string) (head, findings string) {
	m := findingsRE.FindStringSubmatch(body)
	if m == nil {
		return "", ""
	}
	return m[1], m[2]
}

// prKey identifies a PR in a binding.
type prKey struct {
	binding string
	number  int
}

// commentState keeps successful lookup results, including an absent comment
// or a reached limit. Failed reads are retried on a later write and never
// authorize creating a comment.
type commentState struct {
	comment  *github.Comment
	complete bool
	err      error
}

// lookupComment is called with commentMu held.
func (e *Engine) lookupComment(ctx context.Context, b *Binding, pr github.PullRequest) *commentState {
	key := prKey{b.Name, pr.Number}
	if cur, ok := e.comments[key]; ok {
		return cur
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), github.CommentLookupTimeout)
	defer cancel()
	found, complete, err := e.findComment(ctx, b, pr.Number)
	cur := &commentState{comment: found, complete: complete, err: err}
	if err != nil {
		e.commentFailed(ctx, e.prLog(b, pr), b, "list", err)
		return cur
	}
	e.comments[key] = cur
	if found == nil && !complete {
		e.mu.Lock()
		e.commentLimits[key] = true
		e.mu.Unlock()
		e.prLog(b, pr).WarnContext(ctx, "the PR has more comments than the reviewer reads; not adding its own")
	}
	return cur
}

// noteCommentLimits runs on the poller after gate reconciliation. Read the
// current completed check rather than patching an old worker snapshot: a
// hold or enqueue write may have replaced its output during comment lookup.
func (e *Engine) noteCommentLimits(ctx context.Context) {
	e.mu.Lock()
	heads := make(map[prKey]string, len(e.commentLimits))
	for key := range e.commentLimits {
		if head := e.heads[key]; head != "" {
			heads[key] = head
		}
	}
	e.mu.Unlock()
	for i := range e.cfg.Bindings {
		b := &e.cfg.Bindings[i]
		for key, head := range heads {
			if key.binding != b.Name || e.isRunning(headKey{b.Name, head}) {
				continue
			}
			run, err := e.reviewCheck(ctx, b, head)
			if err != nil || run == nil || run.Status != "completed" {
				continue
			}
			e.noteCommentLimit(ctx, b, run, e.prLog(b, github.PullRequest{Number: key.number, HeadSHA: head}))
		}
	}
}

// noteCommentLimit adds the visibility notice after the check has been
// written. It never sends a conclusion or annotations: the gate and its
// findings stand even if this optional write fails.
func (e *Engine) noteCommentLimit(ctx context.Context, b *Binding, run *github.CheckRun, l *slog.Logger) {
	if run == nil || strings.Contains(run.Summary, commentLimitNote) {
		return
	}
	summary := run.Summary + "\n\n" + commentLimitNote
	_, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, github.CheckRunWrite{
		Output: &github.CheckRunOutput{Title: run.Title, Summary: summary, Text: run.Text},
	})
	if err != nil {
		e.ghError(ctx, l, b, "update_check", err)
		return
	}
	run.Summary = summary
}

// noteHead records the head a listing shows for a PR. The comment follows
// only that head, so a write for an earlier one cannot overwrite it.
func (e *Engine) noteHeads(b *Binding, prs []github.PullRequest, complete bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	listed := map[int]bool{}
	for _, pr := range prs {
		if pr.BaseRef == b.BaseBranch {
			e.heads[prKey{b.Name, pr.Number}] = pr.HeadSHA
			listed[pr.Number] = true
		}
	}
	if complete {
		for k := range e.heads {
			if k.binding == b.Name && !listed[k.number] {
				delete(e.heads, k)
			}
		}
	}
}

func (e *Engine) listedHead(k prKey) string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.heads[k]
}

// queueMirror snapshots display data and hands it to one ordered writer.
// The queue lock is never held during I/O or while waiting on commentMu.
// In particular, stopping a review for a shared-head hold cannot wait for
// its comment, and polling can revoke merge-group passes during lookup.
func (e *Engine) queueMirror(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, u *CommentUpdate) {
	if u == nil || e.cfg.Comments == nil || e.cfg.CommentAuthor == "" {
		return
	}
	update := *u
	if u.Findings != nil {
		findings := *u.Findings
		update.Findings = &findings
	}
	if u.Gate != nil {
		gate := *u.Gate
		gate.Paths = slices.Clone(gate.Paths)
		gate.Labels = slices.Clone(gate.Labels)
		gate.Findings = slices.Clone(gate.Findings)
		update.Gate = &gate
	}
	var link *github.CheckRun
	if run != nil {
		link = &github.CheckRun{URL: run.URL}
	}
	e.commentQueueMu.Lock()
	defer e.commentQueueMu.Unlock()
	key := prKey{b.Name, pr.Number}
	if run != nil {
		e.commentReplays[key] = run.ID
	}
	e.commentQueue = append(e.commentQueue, func() {
		if !e.mirror(ctx, b, pr, link, &update) && run != nil {
			e.commentQueueMu.Lock()
			if e.commentReplays[key] == run.ID {
				delete(e.commentReplays, key)
			}
			e.commentQueueMu.Unlock()
		}
	})
	if e.commentWorker {
		return
	}
	e.commentWorker = true
	e.commentWG.Add(1)
	go func() {
		defer e.commentWG.Done()
		for {
			e.commentQueueMu.Lock()
			if len(e.commentQueue) == 0 {
				e.commentWorker = false
				e.commentQueueMu.Unlock()
				return
			}
			write := e.commentQueue[0]
			e.commentQueue[0] = nil
			e.commentQueue = e.commentQueue[1:]
			e.commentQueueMu.Unlock()
			write()
		}
	}()
}

// mirror shows a review check write in the PR's comment, when comments are
// configured. It writes only for the PR's current head and edits only a
// comment CommentAuthor wrote. A comment is created with its first verdict,
// so its one notification carries the verdict, and is only ever edited
// after that. A failed write is logged and counted; the check's conclusion
// is unaffected. Completed snapshots retry on the next poll or restart.
func (e *Engine) mirror(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, u *CommentUpdate) bool {
	if u == nil || e.cfg.Comments == nil || e.cfg.CommentAuthor == "" {
		return true
	}
	key := prKey{b.Name, pr.Number}
	l := e.prLog(b, pr)
	e.commentMu.Lock()
	defer e.commentMu.Unlock()
	if head := e.listedHead(key); head != "" && head != pr.HeadSHA {
		l.DebugContext(ctx, "the PR has a newer head; its comment follows that one")
		return true
	}
	state := e.lookupComment(ctx, b, pr)
	cur := state.comment
	if state.err != nil {
		return false
	}
	// Polling continues during lookup; a push may have superseded this job.
	if head := e.listedHead(key); head != "" && head != pr.HeadSHA {
		return true
	}
	if cur == nil && !state.complete {
		return true
	}
	if cur == nil && u.Verdict == VerdictReviewing {
		return true
	}
	findingsHead, findings := "", ""
	if cur != nil {
		findingsHead, findings = parseFindings(cur.Body)
	}
	if u.Findings != nil {
		findingsHead, findings = pr.HeadSHA, *u.Findings
	}
	url := ""
	if run != nil {
		url = run.URL
	}
	body := renderComment(pr.HeadSHA, u, findings, findingsHead, e.cfg.CheckName, url)
	if u.body != "" {
		body = u.body
		if storedHead, _ := parseFindings(body); storedHead == "" {
			// A hold or failed review without a stored predecessor can still
			// retain findings from the existing comment, including legacy ones.
			body += renderCommentFindings(pr.HeadSHA, findings, findingsHead)
		}
		body = clip(body, maxComment-1000) + commentFooter(pr.HeadSHA, e.cfg.CheckName, url)
	}
	if cur != nil && cur.Body == body {
		return true
	}
	// The page search has its own budget. Start the write deadline only
	// after lookup and lock acquisition, so neither can consume it.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), commentTimeout)
	defer cancel()
	if cur != nil {
		_, err := e.cfg.Comments.UpdateIssueComment(ctx, b.repo(), cur.ID, body)
		switch {
		case err == nil:
			state.comment = &github.Comment{ID: cur.ID, Author: cur.Author, Body: body}
			e.m.CommentWrites.WithLabelValues(b.Name, "updated").Inc()
			return true
		case !errors.Is(err, github.ErrNotFound):
			e.commentFailed(ctx, l, b, "update", err)
			return false
		}
		state.comment, state.complete = nil, true // Deleted by someone: post it again.
		if u.Verdict == VerdictReviewing {
			return true
		}
	}
	created, err := e.cfg.Comments.CreateIssueComment(ctx, b.repo(), pr.Number, body)
	if err != nil {
		// GitHub may have created it even though its response was lost.
		// Rediscover it before another write can try to create a duplicate.
		delete(e.comments, key)
		e.commentFailed(ctx, l, b, "create", err)
		return false
	}
	state.comment = &github.Comment{ID: created.ID, Author: e.cfg.CommentAuthor, Body: body}
	e.m.CommentWrites.WithLabelValues(b.Name, "created").Inc()
	l.InfoContext(ctx, "review comment posted", "comment", created.ID, "verdict", string(u.Verdict))
	return true
}

// findComment is the oldest comment on the PR that CommentAuthor wrote and
// that starts with the comment marker, or nil. complete is false when the
// listing stopped before the PR's last comment.
func (e *Engine) findComment(ctx context.Context, b *Binding, number int) (*github.Comment, bool, error) {
	all, complete, err := e.cfg.Comments.IssueComments(ctx, b.repo(), number)
	if err != nil {
		return nil, false, err
	}
	for _, c := range all {
		if strings.EqualFold(c.Author, e.cfg.CommentAuthor) && strings.HasPrefix(c.Body, commentMarker) {
			return &c, complete, nil
		}
	}
	return nil, complete, nil
}

func (e *Engine) commentFailed(ctx context.Context, l *slog.Logger, b *Binding, call string, err error) {
	e.m.CommentWrites.WithLabelValues(b.Name, "error").Inc()
	l.WarnContext(ctx, "the review comment was not written; the check run stands", "call", call, "error", err)
}

// ownComment reports a comment the reviewer wrote, so its own text never
// reaches the agent as a trusted comment.
func (e *Engine) ownComment(c github.Comment) bool {
	return e.cfg.CommentAuthor != "" && strings.EqualFold(c.Author, e.cfg.CommentAuthor) && strings.HasPrefix(c.Body, commentMarker)
}

// mdPara is agent or PR text as comment paragraphs: each paragraph one
// line (mdInline), the whole clipped to n bytes first.
func mdPara(s string, n int) string {
	s = strings.ReplaceAll(clip(strings.TrimSpace(s), n), "\r\n", "\n")
	var out []string
	for _, p := range paraBreak.Split(s, -1) {
		if p = mdInline(p, n, false); p != "" {
			out = append(out, p)
		}
	}
	return strings.Join(out, "\n\n")
}

var paraBreak = regexp.MustCompile(`\n[ \t]*\n`)

// mdInline is agent or PR text for one line of a comment: collapsed to one
// line, clipped and without service markers (oneLine), with no HTML tag and
// no mention outside code spans. In a table cell, pipes are escaped, inside
// code spans too, as GitHub's tables require. Code spans keep their text.
func mdInline(s string, n int, table bool) string {
	s = oneLine(s, n)
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == '`' {
			j := i
			for j < len(s) && s[j] == '`' {
				j++
			}
			if end := closingRun(s, j, j-i); end >= 0 {
				span := s[i:end]
				if table {
					span = strings.ReplaceAll(span, "|", `\|`)
				}
				b.WriteString(span)
				i = end
				continue
			}
			b.WriteString(s[i:j])
			i = j
			continue
		}
		switch c := s[i]; {
		case c == '<':
			b.WriteString("&lt;")
		case c == '>':
			b.WriteString("&gt;")
		case c == '|' && table:
			b.WriteString(`\|`)
		case c == '@' && i+1 < len(s) && mentionByte(s[i+1]):
			b.WriteString("@​") // a zero-width space: shown as typed, notifies no one
		default:
			b.WriteByte(c)
		}
		i++
	}
	return b.String()
}

// htmlInline is agent or PR text for one line inside an HTML element, where
// GitHub renders no Markdown: escaped, with code spans as <code>.
func htmlInline(s string, n int) string {
	s = oneLine(s, n)
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == '`' {
			j := i
			for j < len(s) && s[j] == '`' {
				j++
			}
			if end := closingRun(s, j, j-i); end >= 0 {
				b.WriteString("<code>" + htmlEscape(strings.TrimSpace(s[j:end-(j-i)])) + "</code>")
				i = end
				continue
			}
			b.WriteString(s[i:j])
			i = j
			continue
		}
		k := strings.IndexByte(s[i:], '`')
		if k < 0 {
			k = len(s) - i
		}
		b.WriteString(htmlEscape(s[i : i+k]))
		i += k
	}
	return b.String()
}

// htmlEscape escapes text for HTML and breaks mentions in it.
func htmlEscape(s string) string {
	s = html.EscapeString(s)
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		b.WriteByte(s[i])
		if s[i] == '@' && i+1 < len(s) && mentionByte(s[i+1]) {
			b.WriteString("​")
		}
	}
	return b.String()
}

// codeText is text for a Markdown code span: one line, without backticks.
func codeText(s string) string {
	return strings.ReplaceAll(oneLine(s, maxTitle), "`", "'")
}

// closingRun is the end of the first run of exactly n backticks at or after
// from, which closes a code span; -1 when there is none.
func closingRun(s string, from, n int) int {
	for i := from; i < len(s); {
		if s[i] != '`' {
			i++
			continue
		}
		j := i
		for j < len(s) && s[j] == '`' {
			j++
		}
		if j-i == n {
			return j
		}
		i = j
	}
	return -1
}

func mentionByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
}
