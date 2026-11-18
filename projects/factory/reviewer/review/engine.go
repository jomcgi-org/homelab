// Package review is the factory reviewer's loop. It polls each binding's
// open PRs into the base branch, keeps a `factory review` check run on every
// head, sends each new head to the binding's kagent reviewer agent,
// completes the check from the agent's findings and enqueues heads that pass
// with required CI green. Check runs hold review state; a private volume
// keeps goal replies for audit
// (projects/factory/ARCHITECTURE.md, "Merge authority"). It holds the
// reviewer App's token; the agent never sees it.
package review

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/config"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
)

// GitHub is the part of the GitHub client the reviewer uses.
type GitHub interface {
	PullRequestAppComments(ctx context.Context, repo string, number int, author string, appID int64, limit int) ([]github.AppComment, int, bool, error)
	ListPullRequests(ctx context.Context, repo, base string) ([]github.PullRequest, bool, error)
	ListCheckRuns(ctx context.Context, repo, sha string, f github.CheckRunFilter) ([]github.CheckRun, error)
	CreateCheckRun(ctx context.Context, repo string, w github.CheckRunWrite) (*github.CheckRun, error)
	UpdateCheckRun(ctx context.Context, repo string, id int64, w github.CheckRunWrite) (*github.CheckRun, error)
	PullRequestFiles(ctx context.Context, repo string, number int) ([]github.PullRequestFile, error)
	PullRequestHeads(ctx context.Context, repo string, number int, f github.CheckRunFilter) (*github.HeadHistory, error)
	PullRequestReviews(ctx context.Context, repo string, number int) ([]github.PullRequestReview, error)
	HeadRefActivity(ctx context.Context, repo, branch string) ([]github.RefActivity, error)
	PullRequestCommits(ctx context.Context, repo string, number int, head string) ([]github.PullRequestCommit, error)
	PullRequestLabelEvents(ctx context.Context, repo string, number int) ([]github.LabelEvent, error)
	PullRequestCreationBoundary(ctx context.Context, repo string) (time.Time, error)
	PullRequestsUpdatedSince(ctx context.Context, repo string, since time.Time) ([]github.UpdatedPullRequest, bool, error)
	CommitPullRequests(ctx context.Context, repo, sha string) ([]github.PullRequest, bool, error)
	PullRequestReviewAcknowledgement(ctx context.Context, repo string, number int, nodeID string) (*github.ReviewAcknowledgement, error)
	PullRequestComments(ctx context.Context, repo string, number int) ([]github.Comment, error)
	CompareCommits(ctx context.Context, repo, base, head string) (*github.CommitComparison, error)
	Statuses(ctx context.Context, repo, sha string) ([]github.Status, error)
	PullRequestQueue(ctx context.Context, repo string, number int) (*github.PullRequestQueue, error)
	EnqueuePullRequest(ctx context.Context, repo, nodeID, headSHA string) (string, error)
	MergeQueueEntries(ctx context.Context, repo, branch string) ([]github.MergeQueueEntry, error)
	CitedPullRequest(ctx context.Context, repo string, number int) (*github.CitedPullRequest, error)
	RulesReader
}

// Tasks is the part of the kagent A2A client the reviewer uses.
type Tasks interface {
	Send(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error)
	GetTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	CancelTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	// ListTasks lists one session's tasks, without history, before the
	// session is resumed.
	ListTasks(ctx context.Context, agent kagent.AgentRef, contextID, state, pageToken string) (*kagent.TaskPage, error)
}

// Quota reads the shared Codex grant before the reviewer sends work.
type Quota interface {
	Quota(context.Context) (github.QuotaReading, error)
}

// Check must succeed on a head before it is enqueued: a check run named
// Name from the GitHub App AppID, or a commit status with context Name set
// by the login StatusCreator.
type Check struct {
	Name          string
	AppID         int64
	StatusCreator string
}

// Binding is one repository whose PRs into BaseBranch the kagent Agent
// reviews, with its gates (see config.Binding).
type Binding struct {
	startupBefore          *time.Time
	Name                   string
	Owner                  string
	Repo                   string
	BaseBranch             string
	Agent                  kagent.AgentRef
	RequiredChecks         []Check
	SkipLabels             []string
	EnqueueAuthors         []string
	HumanGatePaths         []string
	HumanGateLabels        []string
	HumanApprovers         []string
	TrustedCommenters      []string
	ItsaplanProject        string
	BranchPrefix           string
	TrustedItsaplanAuthors []string
	ContextDocs            []string
	GeneratedPaths         []string
	MaxDiffLines           int
	MaxDiffFiles           int
}

func (b *Binding) repo() string { return b.Owner + "/" + b.Repo }

func (b *Binding) skipped(pr *github.PullRequest) bool {
	for _, l := range pr.Labels {
		if containsFold(b.SkipLabels, l) {
			return true
		}
	}
	return false
}

func containsFold(list []string, s string) bool {
	return slices.ContainsFunc(list, func(x string) bool { return strings.EqualFold(x, s) })
}

// Config holds the reviewer's settings.
type Config struct {
	Itsaplan    Itsaplan
	MACSecret   func() (string, error) // The evidence MAC key's input; never log or publish it.
	Semgrep     *Semgrep
	CheckName   string
	AppID       int64
	Concurrency int
	// TaskPoll spaces reads of a running task; Reissue spaces resends of a
	// message kagent has not taken, and retries of failed reads.
	TaskPoll time.Duration
	Reissue  time.Duration
	// Timeout bounds one attempt: its task's run from the send, and the
	// reads and sends before it.
	Timeout time.Duration
	// RetryBackoff spaces the attempts of one review after one ends without
	// a report; a review makes at most MaxAttempts.
	RetryBackoff time.Duration
	// MaxSends bounds the sends of one attempt's message that kagent does
	// not answer with a task. One final send follows DispatchWindow after
	// the last, once nothing sent earlier can still start, before the
	// attempt ends without a report.
	MaxSends       int
	DispatchWindow time.Duration
	// EnqueueBackoff spaces enqueue attempts for one head after a failure.
	EnqueueBackoff time.Duration
	// RuleIDs are scoped rule names that, with ContractRules, bound the rule
	// label of the findings metrics.
	RuleIDs  []string
	Bindings []Binding
	Now      func() time.Time
	// Comments, with CommentAuthor (the reviewer App's bot login), keeps one
	// comment on each PR showing its review; either unset posts none.
	Comments      Comments
	CommentAuthor string
	// Sessions reads kagent sessions, so a later head's review can resume
	// the session that reviewed the previous head; nil starts every review
	// in a fresh session.
	Sessions Sessions
	// Cleaner names each review's session after its PR and deletes the
	// sessions no review will resume (see sweep); nil keeps every session
	// until kagent's idle TTL.
	Cleaner SessionCleaner
	// Quota pauses dispatch while the shared Codex grant is exhausted.
	Quota Quota
	// GoalAudit persists private goal replies before their sessions are deleted.
	// Without it a goal check is unavailable.
	GoalAudit GoalAudit
}

// Engine runs the reviewer.
type Engine struct {
	cfg   Config
	gh    GitHub
	tasks Tasks
	m     *metrics.Metrics
	log   *slog.Logger
	rules map[string]bool
	epoch string

	mu           sync.Mutex
	inflight     map[headKey]*running
	pendingHolds map[headKey]*github.CheckRun
	contexts     map[string]*heldContext
	recoveries   map[prKey]recovery
	wg           sync.WaitGroup

	// heads (under mu) is each listed PR's head. commentMu serializes
	// comment writes and guards comments, the reviewer's comment on each PR
	// as last read or written, including absent comments and reached limits.
	heads     map[prKey]string
	commentMu sync.Mutex
	comments  map[prKey]*commentState
	// commentLimits is under mu. Optional comment I/O has its own worker;
	// neither the poller nor review workers wait on commentMu or that worker.
	commentLimits  map[prKey]bool
	commentQueueMu sync.Mutex
	commentQueue   []func()
	commentReplays map[prKey]int64 // Last check queued; failed writes clear it for retry.
	commentWorker  bool
	commentWG      sync.WaitGroup

	// commitRefs (under mu) is the issue key each PR head's commit messages
	// name, "" for none.
	commitRefs map[string]string

	// Only the polling goroutine uses these.
	groups       map[string]string    // merge group commit: the conclusion of its review check
	enqueueAfter map[string]time.Time // repo#pr@sha: next enqueue attempt after a failure

	// Under mu: each binding's last session sweep, and whether one runs.
	swept    map[string]time.Time
	sweeping map[string]bool
}

// headKey identifies a commit; its check needs unambiguous PR context.
type headKey struct {
	binding string
	sha     string
}

type heldContext struct {
	head, token      string
	labels           []string
	shared, recovery bool
}

// retainGate carries PR facts across cached heads without importing approval.
func (e *Engine) retainGate(b *Binding, pr github.PullRequest, g *Gate, fresh bool) (*Gate, bool) {
	if g == nil {
		g = &Gate{ObservedLabels: gateLabels(b, pr.Labels)}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	key := fmt.Sprintf("%s#%d", b.Name, pr.Number)
	m := e.contexts[key]
	if m == nil {
		m = &heldContext{head: pr.HeadSHA, token: g.ContextToken, labels: append([]string(nil), g.Labels...), shared: g.SharedHead, recovery: g.RecoveryEpoch != ""}
		e.contexts[key] = m
	}
	headChanged := m.head != pr.HeadSHA && (m.shared || m.recovery || len(m.labels) > 0)
	changed := fresh || headChanged
	for _, label := range g.Labels {
		if !slices.Contains(m.labels, label) {
			m.labels = append(m.labels, label)
			changed = true
		}
	}
	changed = changed || (g.SharedHead && !m.shared) || (g.RecoveryEpoch != "" && !m.recovery)
	m.shared, m.recovery, m.head = m.shared || g.SharedHead, m.recovery || g.RecoveryEpoch != "", pr.HeadSHA
	if changed && (m.shared || m.recovery || len(m.labels) > 0) {
		m.token = rand.Text()
	}
	if m.token == "" || g.ContextToken == m.token {
		return g, false
	}
	imported := (m.shared && !g.SharedHead) || (m.recovery && g.RecoveryEpoch == "") || slices.ContainsFunc(m.labels, func(l string) bool { return !slices.Contains(g.Labels, l) })
	g.Labels, g.SharedHead, g.ContextToken = append([]string(nil), m.labels...), m.shared, m.token
	if !fresh || headChanged || imported {
		g.RecoveryEpoch, g.RecoveryPR, m.recovery = e.epoch, pr.Number, true
		g.resetApproval(e.cfg.Now())
	}
	return g, true
}

type running struct {
	number int
	cancel context.CancelCauseFunc
	done   chan struct{}
	// unlisted counts the complete listings in a row that left out both
	// the head and the PR.
	unlisted int
}

// closedAfter is how many complete listings in a row must leave out a PR
// before its review is cancelled as closed: a PR that closes between two
// page reads shifts the next one off both pages.
const closedAfter = 2

var (
	errSuperseded    = errors.New("a newer head of the PR superseded this one")
	errClosed        = errors.New("the PR closed or no longer targets the base branch")
	errDuplicateHead = errors.New("multiple ready PRs share this head")
)

// New builds an engine, filling unset settings with defaults.
func New(cfg Config, gh GitHub, tasks Tasks, m *metrics.Metrics, log *slog.Logger) *Engine {
	if cfg.CheckName == "" {
		cfg.CheckName = "factory review"
	}
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 2
	}
	if cfg.TaskPoll <= 0 {
		cfg.TaskPoll = 15 * time.Second
	}
	if cfg.Reissue <= 0 {
		cfg.Reissue = 15 * time.Second
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 45 * time.Minute
	}
	if cfg.RetryBackoff <= 0 {
		cfg.RetryBackoff = 5 * time.Minute
	}
	if cfg.MaxSends <= 0 {
		cfg.MaxSends = 20
	}
	if cfg.DispatchWindow <= 0 {
		// The send's timeout, kagent's two-minute dispatch reservation and
		// 30 s of clock skew, as the connector waits.
		cfg.DispatchWindow = kagent.DefaultTimeout + 2*time.Minute + 30*time.Second
	}
	if cfg.EnqueueBackoff <= 0 {
		cfg.EnqueueBackoff = 5 * time.Minute
	}
	if cfg.Semgrep != nil && cfg.Semgrep.Wait <= 0 {
		copy := *cfg.Semgrep
		copy.Wait = 10 * time.Minute
		cfg.Semgrep = &copy
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	cfg.Bindings = append([]Binding(nil), cfg.Bindings...)
	for i := range cfg.Bindings {
		b := &cfg.Bindings[i]
		b.startupBefore = nil
		if b.MaxDiffLines <= 0 {
			b.MaxDiffLines = 2500
		}
		if b.MaxDiffFiles <= 0 {
			b.MaxDiffFiles = 150
		}
	}
	if m == nil {
		m = metrics.New(nil)
	}
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	rules := map[string]bool{}
	for _, r := range append(append([]string(nil), ContractRules...), cfg.RuleIDs...) {
		rules[strings.ToLower(r)] = true
	}
	return &Engine{
		cfg: cfg, gh: gh, tasks: tasks, m: m, log: log, rules: rules, epoch: rand.Text(),
		inflight: map[headKey]*running{}, pendingHolds: map[headKey]*github.CheckRun{}, contexts: map[string]*heldContext{}, recoveries: map[prKey]recovery{},
		groups: map[string]string{}, enqueueAfter: map[string]time.Time{},
		heads: map[prKey]string{}, comments: map[prKey]*commentState{}, commentLimits: map[prKey]bool{}, commentReplays: map[prKey]int64{},
		swept: map[string]time.Time{}, sweeping: map[string]bool{},
	}
}

// Run polls every poll until ctx ends, then waits for running reviews to
// return. A review that shutdown interrupts leaves its check run in
// progress, and the next process re-attaches to it.
func (e *Engine) Run(ctx context.Context, poll time.Duration) error {
	t := time.NewTicker(poll)
	defer t.Stop()
	for {
		e.Tick(ctx)
		select {
		case <-ctx.Done():
			e.Wait()
			return ctx.Err()
		case <-t.C:
		}
	}
}

// Wait drains reviews and their optional comment writes after polling stops.
func (e *Engine) Wait() {
	e.wg.Wait()
	e.commentWG.Wait()
}

// Tick polls every binding once. Reviews it starts run in the background
// under ctx.
func (e *Engine) Tick(ctx context.Context) {
	for i := range e.cfg.Bindings {
		if ctx.Err() != nil {
			return
		}
		e.tickBinding(ctx, &e.cfg.Bindings[i])
	}
	// The worker may discover a comment limit after a check was completed.
	// Refresh its notice only after every binding's merge groups reconcile.
	e.noteCommentLimits(ctx)
}

type candidate struct {
	pr  github.PullRequest
	run *github.CheckRun
}

func (e *Engine) tickBinding(ctx context.Context, b *Binding) {
	repo := b.repo()
	bl := e.log.With("binding", b.Name, "repo", repo)
	if b.startupBefore == nil {
		boundary, err := e.gh.PullRequestCreationBoundary(ctx, repo)
		if err != nil {
			e.ghError(ctx, bl, b, "creation_boundary", err)
			e.mergeGroups(ctx, b, nil)
			return
		}
		b.startupBefore = &boundary
	}
	prs, complete, err := e.gh.ListPullRequests(ctx, repo, b.BaseBranch)
	if err != nil {
		e.ghError(ctx, bl, b, "list_pulls", err)
		e.mergeGroups(ctx, b, nil)
		return
	}
	if !complete {
		bl.WarnContext(ctx, "the open PR listing is longer than the client reads; PRs past it are not reviewed this poll", "listed", len(prs))
	}
	e.supersede(b, prs, complete)
	e.noteHeads(b, prs, complete)
	e.sweepSessions(ctx, b, prs, complete, bl)
	prioritize(b, prs)
	counts, labels := readyHeads(b, prs)
	scan := e.recoveryScan(b, prs)

	var attach, start []candidate
	reviews := map[int]*github.CheckRun{}
	heldHeads := map[string]*github.CheckRun{}
	for _, pr := range prs {
		if pr.BaseRef != b.BaseBranch || pr.HeadSHA == "" || pr.Draft {
			continue
		}
		if !complete || counts[pr.HeadSHA] > 1 || e.pendingHold(headKey{b.Name, pr.HeadSHA}) != nil {
			e.retainGate(b, pr, &Gate{SharedHead: true, Labels: labels[pr.HeadSHA]}, false)
			cur, held := heldHeads[pr.HeadSHA]
			if !held {
				// Await the worker so it cannot overwrite the hold.
				if !e.stopSharedHead(ctx, b, pr.HeadSHA) {
					return
				}
				if run, err := e.reviewCheck(ctx, b, pr.HeadSHA); err == nil {
					cur = e.holdSharedHead(ctx, b, pr, run, labels[pr.HeadSHA])
				}
				heldHeads[pr.HeadSHA] = cur
			}
			reviews[pr.Number] = cur
			e.replayComment(ctx, b, pr, cur)
			continue
		}
		if b.skipped(&pr) || e.isRunning(headKey{b.Name, pr.HeadSHA}) {
			continue
		}
		run, err := e.reviewCheck(ctx, b, pr.HeadSHA)
		if err != nil {
			reviews[pr.Number] = nil // Unknown approval must also fail a queued group.
			continue
		}
		reviews[pr.Number] = run
		revoked, unread := e.decisionsChanged(ctx, b, pr, run)
		foreign := run != nil && run.ExternalID != MessageID(repo, pr.Number, pr.HeadSHA)
		recovered := e.recoverPR(ctx, b, pr, scan)
		switch {
		case !recovered && (sharedHeadHeld(run) || !foreign):
			reviews[pr.Number] = nil // An unconfirmed context must also fail a queued group.
		case sharedHeadHeld(run):
			// A resolved duplicate needs its remaining PR reviewed afresh.
			start = append(start, candidate{pr, run})
		case foreign:
			// A replacement PR cannot inherit an earlier PR's unknown holds.
			if run.Status != "completed" {
				e.cancelTask(ctx, b, RecordedTask(run.Summary).ID, e.prLog(b, pr))
			}
			reviews[pr.Number] = e.holdSharedHead(ctx, b, pr, run, labels[pr.HeadSHA])
			if reviews[pr.Number] != nil && recovered {
				start = append(start, candidate{pr, reviews[pr.Number]})
			}
		case run == nil || (run.Status == "completed" && run.Conclusion == ConclusionCancelled):
			// A cancelled review is no review: the head is current again.
			start = append(start, candidate{pr, nil})
		case run.Status != "completed":
			attach = append(attach, candidate{pr, run})
		case unread:
			reviews[pr.Number] = nil // An unconfirmed decision must not let a pass enqueue.
		case revoked:
			// Joe edited or deleted a decision this review relied on: withdraw
			// the pass, and review the head again until a review runs. Its
			// State keeps the decisions, so every poll retries.
			if run.Conclusion == ConclusionSuccess {
				updated, err := e.gh.UpdateCheckRun(ctx, repo, run.ID, github.CheckRunWrite{
					Status: "completed", Conclusion: ConclusionFailure,
					Output: &github.CheckRunOutput{
						Title:   "Joe's decision changed",
						Summary: "A decision of Joe's that this review relied on was edited or deleted. A new review of this head is required before it can pass.",
						Text:    run.Text,
					},
				})
				if err != nil {
					e.ghError(ctx, e.prLog(b, pr), b, "update_check", err)
				} else {
					run = updated
				}
			}
			reviews[pr.Number] = nil
			start = append(start, candidate{pr, run})
		case e.semgrepChanged(ctx, b, pr, run):
			// Withdraw a stale pass even when all review slots are occupied.
			if run.Conclusion == ConclusionSuccess {
				st, _ := LoadState(run.Text)
				st.Semgrep = nil                     // A new review stays required if the scan changes back.
				st.TaskID = e.sourceTask(b, run, st) // The new summary drops a legacy task reference.
				updated, err := e.gh.UpdateCheckRun(ctx, repo, run.ID, github.CheckRunWrite{
					Status: "completed", Conclusion: ConclusionFailure,
					Output: &github.CheckRunOutput{Title: "Semgrep evidence changed", Summary: "Semgrep evidence changed on this head. A new review is required before it can pass.", Text: encodeState(st)},
				})
				if err != nil {
					e.ghError(ctx, e.prLog(b, pr), b, "update_check", err)
				} else {
					run = updated
				}
			}
			reviews[pr.Number] = nil
			start = append(start, candidate{pr, run})
		default:
			cur, pending, ready := e.reconcileIssues(ctx, b, pr, run)
			reviews[pr.Number] = cur
			if pending {
				if ready && cur != nil {
					start = append(start, candidate{pr, cur})
				}
				continue
			}
			cur, held := e.reconcileGate(ctx, b, pr, cur)
			// A failed write returns nil; do not refetch the stale pass.
			reviews[pr.Number] = cur
			if !held && cur != nil && cur.Conclusion == ConclusionSuccess {
				reviews[pr.Number] = e.maybeEnqueue(ctx, b, pr, cur)
			}
			e.replayComment(ctx, b, pr, reviews[pr.Number])
		}
	}
	// Reviews already in progress (after a restart) go before new heads.
	waiting := append(attach, start...)
	for i, c := range waiting {
		if e.begin(ctx, b, c.pr, c.run) {
			bl.DebugContext(ctx, "review slots full", "waiting", len(waiting)-i)
			break
		}
	}
	e.mergeGroups(ctx, b, reviews)
}

// readyHeads includes skipped PRs, which must not borrow a shared head's pass.
func readyHeads(b *Binding, prs []github.PullRequest) (map[string]int, map[string][]string) {
	counts := map[string]int{}
	labels := map[string][]string{}
	for _, pr := range prs {
		if pr.BaseRef == b.BaseBranch && pr.HeadSHA != "" && !pr.Draft {
			counts[pr.HeadSHA]++
			labels[pr.HeadSHA] = append(labels[pr.HeadSHA], gateLabels(b, pr.Labels)...)
		}
	}
	for head, ls := range labels {
		sort.Strings(ls)
		labels[head] = slices.Compact(ls)
	}
	return counts, labels
}

func sharedHeadHeld(run *github.CheckRun) bool {
	return run != nil && run.Status == "completed" && run.Conclusion == ConclusionFailure && strings.Contains(run.Summary, duplicateHeadMarker)
}

func sharedHeadOutcome(prev *github.CheckRun, labels []string, now time.Time) Outcome {
	o := Outcome{Conclusion: ConclusionFailure, Output: github.CheckRunOutput{
		Title: "Unambiguous PR context required",
		Summary: "This commit has duplicate PRs, an incomplete PR listing, or a review belonging to another PR. Close, draft or move duplicates; the listing must be complete. " +
			"The remaining PR will receive a new review and require fresh human approval.\n\n" + duplicateHeadMarker + "\n\n" + gateOnlyMarker,
	}, Comment: &CommentUpdate{Verdict: VerdictHold, Headline: "Another ready PR shares this commit", Note: "Close, draft or move the duplicate."}}
	var st State
	if prev != nil {
		st, _ = LoadState(prev.Text) // Keep prior findings available to a later head.
	}
	if st.Gate == nil {
		st.Gate = &Gate{}
	}
	st.Gate.SharedHead = true
	for _, label := range labels {
		if !slices.Contains(st.Gate.Labels, label) {
			st.Gate.Labels = append(st.Gate.Labels, label)
			st.Gate.resetApproval(now)
		}
	}
	o.Output.Text = encodeState(st)
	o.Output.Summary = keepEnqueueHistory(o.Output.Summary, prev)
	return o
}

// stopSharedHead also waits for any check write already in progress.
func (e *Engine) stopSharedHead(ctx context.Context, b *Binding, sha string) bool {
	e.mu.Lock()
	r := e.inflight[headKey{b.Name, sha}]
	if r != nil {
		r.cancel(errDuplicateHead)
	}
	e.mu.Unlock()
	if r != nil {
		select {
		case <-r.done:
		case <-ctx.Done():
			return false
		}
	}
	return true
}

func (e *Engine) holdSharedHead(ctx context.Context, b *Binding, pr github.PullRequest, prev *github.CheckRun, labels []string) *github.CheckRun {
	key := headKey{b.Name, pr.HeadSHA}
	pending := e.pendingHold(key)
	if pending != nil {
		prev = pending
	}
	now := e.cfg.Now().UTC()
	o := sharedHeadOutcome(prev, labels, now)
	previous := ""
	if prev != nil {
		previous = prev.Text
	}
	o.Output.Text = recordComment(o.Output.Text, previous, pr.HeadSHA, o.Comment)
	if pending == nil && sharedHeadHeld(prev) && o.Output.Text == prev.Text {
		return prev
	}
	run, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
		Status: "completed", Conclusion: o.Conclusion, StartedAt: &now, CompletedAt: &now, Output: &o.Output,
	})
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "create_check", err)
		// Revoke an existing pass even if creating its replacement failed.
		if prev != nil && prev.ID > 0 {
			run, err = e.gh.UpdateCheckRun(ctx, b.repo(), prev.ID, github.CheckRunWrite{
				Status: "completed", Conclusion: o.Conclusion, CompletedAt: &now, Output: &o.Output,
			})
		}
		if err != nil {
			e.rememberHold(key, prev, o)
			return nil
		}
	}
	e.mu.Lock()
	delete(e.pendingHolds, key)
	e.mu.Unlock()
	e.queueCompletedComment(ctx, b, pr, run, o.Output.Text, o.Comment)
	return run
}

func (e *Engine) pendingHold(key headKey) *github.CheckRun {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.pendingHolds[key]
}

// Pending intent fences this process; startup recovery fences after a restart.
func (e *Engine) rememberHold(key headKey, prev *github.CheckRun, o Outcome) {
	run := &github.CheckRun{HeadSHA: key.sha}
	if prev != nil {
		*run = *prev
	}
	run.Status, run.Conclusion = "completed", o.Conclusion
	run.Summary, run.Text = o.Output.Summary, o.Output.Text
	e.mu.Lock()
	e.pendingHolds[key] = run
	e.mu.Unlock()
}

// prioritize orders a listing for the review slots: PRs whose authors the
// binding enqueues first, then the most recently updated.
func prioritize(b *Binding, prs []github.PullRequest) {
	sort.SliceStable(prs, func(i, j int) bool {
		ei, ej := containsFold(b.EnqueueAuthors, prs[i].Author), containsFold(b.EnqueueAuthors, prs[j].Author)
		if ei != ej {
			return ei
		}
		return prs[i].UpdatedAt.After(prs[j].UpdatedAt)
	})
}

// supersede cancels running reviews of heads no open PR into the base
// still has. Another PR at the same commit keeps its review running. A PR
// listed at another head superseded its review at once; one left out of
// closedAfter complete listings in a row closed. An incomplete listing
// proves no PR closed.
func (e *Engine) supersede(b *Binding, prs []github.PullRequest, complete bool) {
	heads := map[string]bool{}
	open := map[int]bool{}
	for _, pr := range prs {
		if pr.BaseRef == b.BaseBranch {
			heads[pr.HeadSHA] = true
			open[pr.Number] = true
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	for key, r := range e.inflight {
		switch {
		case key.binding != b.Name:
		case heads[key.sha]:
			r.unlisted = 0
		case open[r.number]:
			r.cancel(errSuperseded)
		case complete:
			if r.unlisted++; r.unlisted >= closedAfter {
				r.cancel(errClosed)
			}
		}
	}
}

func (e *Engine) isRunning(key headKey) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	_, ok := e.inflight[key]
	return ok
}

func (e *Engine) done(key headKey) {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.inflight, key)
	e.m.InFlight.Set(float64(len(e.inflight)))
}

func (e *Engine) prLog(b *Binding, pr github.PullRequest) *slog.Logger {
	return e.log.With("binding", b.Name, "repo", b.repo(), "pr", pr.Number, "sha", pr.HeadSHA)
}

// reviewCheck is the latest review check run from the reviewer App on a
// commit, or nil.
func (e *Engine) reviewCheck(ctx context.Context, b *Binding, sha string) (*github.CheckRun, error) {
	runs, err := e.gh.ListCheckRuns(ctx, b.repo(), sha, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID})
	if err != nil {
		e.ghError(ctx, e.log.With("binding", b.Name, "repo", b.repo(), "sha", sha), b, "check_runs", err)
		return nil, err
	}
	var latest *github.CheckRun
	for i := range runs {
		r := &runs[i]
		if r.Name != e.cfg.CheckName || r.AppID != e.cfg.AppID || r.HeadSHA != sha {
			continue
		}
		if latest == nil || r.ID > latest.ID {
			latest = r
		}
	}
	return latest, nil
}

// recovery is what this process established about a PR whose observations
// an earlier process may have held only in memory. A restart loses only the
// shared-head observations whose check writes all failed: labels have
// GitHub's label history, and every other hold is in a check run's state.
type recovery int

const (
	recoveryPending recovery = iota // not read yet
	recoveryClear                   // durable reads show nothing lost
	recoveryShared                  // another PR is or was at one of its heads
	recoveryUnknown                 // a read was bounded; its context stays unconfirmed
)

func (r recovery) String() string {
	return [...]string{"pending", "clear", "shared", "unknown"}[r]
}

// recoveryOf is the PR's recovery. A PR created after startup lost nothing.
func (e *Engine) recoveryOf(b *Binding, pr github.PullRequest) recovery {
	if b.startupBefore != nil && !pr.CreatedAt.IsZero() && pr.CreatedAt.After(*b.startupBefore) {
		return recoveryClear
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.recoveries[prKey{b.Name, pr.Number}]
}

// recoveryGate applies the PR's recovery to its gate. A gate an earlier
// process held only for recovery clears once this process confirms the PR's
// context; a recovery hold with any other reason stays as it is.
func (e *Engine) recoveryGate(b *Binding, pr github.PullRequest, gate *Gate) (*Gate, bool) {
	r := e.recoveryOf(b, pr)
	if gate == nil {
		if r == recoveryClear {
			return nil, false
		}
		gate = &Gate{ObservedLabels: gateLabels(b, pr.Labels)}
	}
	hold := func() bool {
		gate.RecoveryEpoch, gate.RecoveryPR = e.epoch, pr.Number
		gate.resetApproval(e.cfg.Now())
		return true
	}
	changed := false
	if gate.RecoveryEpoch != "" && gate.RecoveryPR != pr.Number {
		changed = hold() // Another PR's recovery baseline cannot approve this PR.
	}
	switch r {
	case recoveryClear:
		if gate.recoveryOnly() && gate.RecoveryEpoch != e.epoch {
			gate.RecoveryEpoch, gate.RecoveryPR = "", 0
			gate.ApprovalAfter, gate.ApprovalSnapshot, gate.ApprovalNonce, gate.Approver = time.Time{}, nil, "", ""
			changed = true
		}
	case recoveryShared:
		if !gate.SharedHead {
			gate.SharedHead = true
			gate.resetApproval(e.cfg.Now())
			changed = true
		}
	default:
		if gate.RecoveryEpoch == "" {
			changed = hold()
		}
	}
	return gate, changed
}

// recentPRs is what one poll's recoveries compare heads with: its open PR
// listing, and the PRs updated since the oldest PR it recovers, back to
// covered.
type recentPRs struct {
	open           []github.PullRequest
	since, covered time.Time
	// truncated marks a listing that hit its bound. It skipped the final
	// reread of its first page, so a PR updated during the read can be
	// missing anywhere, and unlisted PRs can share its oldest timestamp.
	truncated bool
	read      bool
	prs       []github.UpdatedPullRequest
	err       error
}

// covers reports whether the listing includes every PR updated at or after
// t. A truncated listing covers nothing.
func (s *recentPRs) covers(t time.Time) bool {
	return !s.truncated && !t.Before(s.covered)
}

// recoveryScan bounds the poll's listing of recently updated PRs by the
// oldest ready PR whose recovery is pending.
func (e *Engine) recoveryScan(b *Binding, prs []github.PullRequest) *recentPRs {
	scan := &recentPRs{open: prs}
	for _, pr := range prs {
		if pr.Draft || pr.HeadSHA == "" || pr.CreatedAt.IsZero() || b.skipped(&pr) {
			continue // Never recovered, so never compared.
		}
		if e.recoveryOf(b, pr) == recoveryPending && (scan.since.IsZero() || pr.CreatedAt.Before(scan.since)) {
			scan.since = pr.CreatedAt
		}
	}
	return scan
}

// recoverPR reads, once per process, whether a restart could have lost a
// hold on the PR. It reports false after a failed read: the PR then gets
// no review, pass or enqueue this poll, and the next poll reads again.
func (e *Engine) recoverPR(ctx context.Context, b *Binding, pr github.PullRequest, scan *recentPRs) bool {
	if e.recoveryOf(b, pr) != recoveryPending {
		return true
	}
	r, err := e.readRecovery(ctx, b, pr, scan)
	if err != nil {
		return false
	}
	e.mu.Lock()
	e.recoveries[prKey{b.Name, pr.Number}] = r
	e.mu.Unlock()
	e.prLog(b, pr).InfoContext(ctx, "review context recovered", "recovery", r.String())
	return true
}

// readRecovery looks for a lost shared-head observation. Every head the
// PR's timeline lists is matched against the heads of the open PRs into the
// base and of every PR updated since the PR opened, in any state and into
// any base; a PR that closed before this one opened never shared with it.
// The open PRs GitHub associates with the current head, into any base,
// show a duplicate that moved on from it or retargeted. A bounded or
// truncated read leaves the context unknown.
func (e *Engine) readRecovery(ctx context.Context, b *Binding, pr github.PullRequest, scan *recentPRs) (recovery, error) {
	l := e.prLog(b, pr)
	if pr.CreatedAt.IsZero() {
		return recoveryUnknown, nil
	}
	hist, err := e.gh.PullRequestHeads(ctx, b.repo(), pr.Number, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID})
	if err != nil {
		e.ghError(ctx, l, b, "pull_request_heads", err)
		return recoveryPending, err
	}
	if hist.Truncated {
		return recoveryUnknown, nil
	}
	if _, _, unknown, err := e.previousStateFrom(ctx, b, pr, hist); err != nil {
		return recoveryPending, err
	} else if unknown != "" {
		return recoveryUnknown, nil // An earlier head's gate may not have carried forward.
	}
	if !scan.read {
		scan.read = true
		var complete bool
		scan.prs, complete, scan.err = e.gh.PullRequestsUpdatedSince(ctx, b.repo(), scan.since)
		switch n := len(scan.prs); {
		case scan.err != nil:
			e.ghError(ctx, l, b, "recent_pulls", scan.err)
		case complete:
			scan.covered = scan.since
		case n > 0:
			scan.covered, scan.truncated = scan.prs[n-1].UpdatedAt, true
		default:
			scan.covered, scan.truncated = time.Unix(1<<40, 0), true // Nothing is covered.
		}
	}
	if scan.err != nil {
		return recoveryPending, scan.err
	}
	if !scan.covers(pr.CreatedAt) {
		return recoveryUnknown, nil
	}
	heads := map[string]bool{pr.HeadSHA: true}
	for _, h := range hist.Heads {
		heads[h.SHA] = true
	}
	for _, q := range scan.prs {
		if q.Number != pr.Number && heads[q.HeadSHA] && (q.ClosedAt.IsZero() || !q.ClosedAt.Before(pr.CreatedAt)) {
			return recoveryShared, nil
		}
	}
	for _, q := range scan.open {
		if q.Number != pr.Number && heads[q.HeadSHA] {
			return recoveryShared, nil
		}
	}
	assoc, complete, err := e.gh.CommitPullRequests(ctx, b.repo(), pr.HeadSHA)
	if err != nil {
		e.ghError(ctx, l, b, "commit_pulls", err)
		return recoveryPending, err
	}
	if !complete {
		return recoveryUnknown, nil
	}
	// Any other open PR containing the head may have shared it, whatever its
	// base or current head: a duplicate can add commits and retarget.
	for _, q := range assoc {
		if q.Number != pr.Number {
			return recoveryShared, nil
		}
	}
	return recoveryClear, nil
}

// begin starts a review in the background, creating its check run first
// when the head has none. It reports true, starting nothing, when every
// slot is taken; a head already under review starts nothing either.
func (e *Engine) begin(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (full bool) {
	// Recheck the diff budget before a quota wait, without a review worker.
	// This also handles checks that an older process paused before sizing.
	if run != nil {
		rec := RecordedTask(run.Summary)
		if !rec.QuotaSince.IsZero() {
			l := e.prLog(b, pr)
			files, err := e.gh.PullRequestFiles(ctx, b.repo(), pr.Number)
			if err != nil {
				e.ghError(ctx, l, b, "files", err)
				return false
			}
			if e.requestSplit(ctx, b, pr, run, measure(b, files), l) || e.waitForQuota(ctx, b, run, rec, l) {
				return false
			}
		}
	}
	key := headKey{b.Name, pr.HeadSHA}
	e.mu.Lock()
	if _, ok := e.inflight[key]; ok {
		e.mu.Unlock()
		return false
	}
	if len(e.inflight) >= e.cfg.Concurrency {
		e.mu.Unlock()
		return true
	}
	rctx, cancel := context.WithCancelCause(ctx)
	r := &running{number: pr.Number, cancel: cancel, done: make(chan struct{})}
	e.inflight[key] = r
	e.m.InFlight.Set(float64(len(e.inflight)))
	e.mu.Unlock()

	l := e.prLog(b, pr)
	kind := "reattach"
	if run == nil || run.Status == "completed" || issuesPending(run) {
		kind = "new"
		now := e.cfg.Now().UTC()
		st := State{Gate: &Gate{Labels: gateLabels(b, pr.Labels), ObservedLabels: gateLabels(b, pr.Labels)}}
		if run != nil {
			if prior, ok := LoadState(run.Text); ok {
				st = prior // Same-head findings and sticky holds survive duplicate recovery.
			}
			if st.Gate == nil {
				st.Gate = &Gate{}
			}
			if sharedHeadHeld(run) {
				st.Gate.SharedHead = true
				st.Gate.resetApproval(now) // Prior approval belonged to an ambiguous PR context.
			}
			st.Gate.observeLabels(gateLabels(b, pr.Labels), now)
			st.IssueDigest, st.IssuesPending = "", false
		}
		st.Gate, _ = e.recoveryGate(b, pr, st.Gate)
		st.Gate, _ = e.retainGate(b, pr, st.Gate, false)
		created, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
			Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
			Status: "in_progress", StartedAt: &now,
			Output: &github.CheckRunOutput{
				Title: "Review in progress", Summary: keepEnqueueHistory(progressSummary(b, nil, recorded{}, ""), run),
				Text: encodeState(st),
			},
		})
		if err != nil {
			e.ghError(ctx, l, b, "create_check", err)
			cancel(nil)
			e.done(key)
			close(r.done)
			return false
		}
		if created.StartedAt.IsZero() {
			created.StartedAt = now
		}
		run = created
		l.InfoContext(ctx, "review check created", "check_run", run.ID)
	} else {
		st, _ := LoadState(run.Text)
		gate, changed := e.recoveryGate(b, pr, st.Gate)
		gate, retained := e.retainGate(b, pr, gate, false)
		changed = changed || retained
		if changed {
			st.Gate = gate
			updated, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, github.CheckRunWrite{Output: &github.CheckRunOutput{
				Title: "Recovering review context", Summary: keepEnqueueHistory(progressSummary(b, gate, RecordedTask(run.Summary), ""), run), Text: encodeState(st),
			}})
			if err != nil {
				e.ghError(ctx, l, b, "update_check", err)
				cancel(nil)
				e.done(key)
				close(r.done)
				return false
			}
			run = updated
		}
		l.InfoContext(ctx, "re-attaching to an in-progress review", "check_run", run.ID)
	}
	e.m.ReviewsStarted.WithLabelValues(b.Name, kind).Inc()
	e.wg.Add(1)
	go func() {
		defer close(r.done)
		defer e.wg.Done()
		defer e.done(key)
		defer cancel(nil)
		if kind == "new" {
			e.queueMirror(rctx, b, pr, run, &CommentUpdate{Verdict: VerdictReviewing})
		}
		e.review(rctx, b, pr, run, l.With("check_run", run.ID))
	}()
	return false
}

// recorded is the attempt an in-progress check run records, its task once
// sent and when it was sent.
type recorded struct {
	Attempt int
	ID      string
	Sent    time.Time
	Issues  string
	// QuotaRetry gives a resumed send a new identity without spending an
	// attempt. QuotaSince persists the wait across service restarts.
	QuotaRetry int
	QuotaSince time.Time
	Semgrep    string
}

// The task marker is written only by the service; markerStart is stripped
// from every text the agent or the PR supplies, and the last marker counts.
var taskMarker = regexp.MustCompile(`<!-- factory-review:task attempt=([0-9]+) id=([^\s>]*) sent=([0-9]*)(?: issues=([a-f0-9]{64}))?(?: semgrep=([a-f0-9]{64}))? -->`)

var quotaMarker = regexp.MustCompile(`<!-- factory-review:quota retry=([0-9]+) since=([0-9]+) -->`)

// RecordedTask is the attempt and kagent task an in-progress review check
// run records.
func RecordedTask(summary string) recorded {
	all := taskMarker.FindAllStringSubmatch(summary, -1)
	if len(all) == 0 {
		return recorded{}
	}
	m := all[len(all)-1]
	n, _ := strconv.Atoi(m[1])
	rec := recorded{Attempt: max(n, 1), ID: m[2], Issues: m[4], Semgrep: m[5]}
	if sec, err := strconv.ParseInt(m[3], 10, 64); err == nil && sec > 0 {
		rec.Sent = time.Unix(sec, 0)
	}
	if all := quotaMarker.FindAllStringSubmatch(summary, -1); len(all) > 0 {
		m := all[len(all)-1]
		rec.QuotaRetry, _ = strconv.Atoi(m[1])
		if ns, err := strconv.ParseInt(m[2], 10, 64); err == nil && ns > 0 {
			rec.QuotaSince = time.Unix(0, ns)
		}
	}
	return rec
}

// progressSummary is an in-progress check's summary: what is running, why
// an earlier attempt ended, the head's human gate, and the attempt marker.
func progressSummary(b *Binding, gate *Gate, rec recorded, note string) string {
	s := fmt.Sprintf("The kagent agent `%s/%s` is reviewing this commit.", b.Agent.Namespace, b.Agent.Name)
	if !rec.QuotaSince.IsZero() {
		s = "Waiting for the shared Codex grant to reset. This review holds no worker and spends no attempt while waiting."
	}
	if note != "" {
		s += "\n\n" + note
	}
	if gate.held() {
		s += "\n\n" + gateFinding(b, gate).Detail
	}
	if rec.Attempt > 0 {
		s += fmt.Sprintf("\n\nAttempt %d of %d", rec.Attempt, MaxAttempts)
		if rec.ID != "" {
			s += fmt.Sprintf(": kagent task `%s`", rec.ID)
		}
		var sent int64
		if !rec.Sent.IsZero() {
			sent = rec.Sent.Unix()
		}
		s += fmt.Sprintf(".\n\n<!-- factory-review:task attempt=%d id=%s sent=%d", rec.Attempt, rec.ID, sent)
		if rec.Issues != "" {
			s += " issues=" + rec.Issues
		}
		if rec.Semgrep != "" {
			s += " semgrep=" + rec.Semgrep
		}
		s += " -->"
	}
	if rec.QuotaRetry > 0 || !rec.QuotaSince.IsZero() {
		var since int64
		if !rec.QuotaSince.IsZero() {
			since = rec.QuotaSince.UnixNano()
		}
		s += fmt.Sprintf("\n\n<!-- factory-review:quota retry=%d since=%d -->", rec.QuotaRetry, since)
	}
	return s
}

// requestSplit reports whether the diff is over budget. Its check can
// complete without a Codex task, even while the grant is exhausted.
func (e *Engine) requestSplit(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, size DiffSize, l *slog.Logger) bool {
	if !size.over(b) {
		return false
	}
	l.WarnContext(ctx, "the PR is over the review budget; asking for a split",
		"lines", size.Lines, "files", size.Files, "generated", size.Generated)
	e.m.SplitRequests.WithLabelValues(b.Name).Inc()
	o := SplitOutcome(b, size, e.footer(b, pr, ""))
	o.Output.Summary += "\n\n" + splitOnlyMarker
	e.finish(ctx, b, pr, run, o, l)
	return true
}

// waitForQuota leaves the check in progress and returns the worker to the
// pool. A failed read never dispatches work; after a quota refusal, only a
// newer usable reading can resume it, as in the connector.
func (e *Engine) waitForQuota(ctx context.Context, b *Binding, run *github.CheckRun, rec recorded, l *slog.Logger) bool {
	if e.cfg.Quota == nil && rec.QuotaSince.IsZero() {
		return false
	}
	note := "Waiting for a usable reading from the broker."
	if e.cfg.Quota != nil {
		q, err := e.cfg.Quota.Quota(ctx)
		if err == nil && !q.Exhausted && (rec.QuotaSince.IsZero() || q.ReceivedAt.After(rec.QuotaSince)) {
			return false
		}
		if err != nil {
			l.WarnContext(ctx, "could not read Codex quota", "error", err)
		} else if q.Exhausted && !q.ResetsAt.IsZero() {
			note = "The broker reports a reset at " + q.ResetsAt.UTC().Format(time.RFC3339) + "."
		}
	}
	rec.Attempt = max(rec.Attempt, 1)
	if rec.QuotaSince.IsZero() {
		rec.QuotaSince = e.cfg.Now()
	}
	e.recordProgress(ctx, b, run, nil, rec, note, l)
	return true
}

// inputs are what a review is judged with, read before its task is sent.
type inputs struct {
	issues    *IssueContext
	goal      *GoalCheck // the head's goal check, or nil
	semgrep   *SemgrepEvidence
	files     []github.PullRequestFile
	size      DiffSize
	gate      *Gate
	prior     State
	priorHead string
	// priorUnknown says why complete earlier review state could not be recovered.
	priorUnknown string
	// since is what changed since priorHead, once compared is set; nil
	// when GitHub no longer has priorHead.
	since    *github.CommitComparison
	compared bool
	// scope is the later-review scope (laterScope), once scoped is set.
	scope  *Scope
	scoped bool
	// plan is the session plan of the last message this process built.
	plan sessionPlan
	// comments are the trusted comments that message put in the prompt;
	// commentsRead is false until it read them in this process.
	comments     []github.Comment
	commentsRead bool
}

// review drives one head's review to a completed check run. It makes at
// most MaxAttempts attempts. Each builds its message once and sends those
// exact bytes until kagent answers with a task, which runs for at most
// Timeout: the first attempt on a later head in the session that reviewed
// the previous one when it can (session.go), else in a fresh session. A
// continuation kagent refuses is rebuilt for a fresh session. An attempt
// that ends without a report (a failed, rejected or timed-out task, an
// unusable reply, or reads that kept failing) is followed by the next one
// after RetryBackoff; the last one fails the check. A restart finds the task the check run records, else
// sends the recorded attempt's message again. A quota refusal instead leaves
// the check in progress, spends no attempt and releases the worker.
func (e *Engine) review(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, l *slog.Logger) {
	local := *run
	run = &local
	rec := RecordedTask(run.Summary)
	attempt := max(rec.Attempt, 1)
	began := e.cfg.Now()
	if rec.ID != "" && rec.Sent.IsZero() {
		rec.Sent = run.StartedAt
		if rec.Sent.IsZero() {
			rec.Sent = began
		}
	}
	var (
		in       *inputs
		msg      *kagent.Message
		sends    int
		lastSend time.Time
		refused  = rec.QuotaRetry > 0
	)
	// next ends the attempt without a report. It reports true when that was
	// the last attempt and the check is complete.
	next := func(reason string) bool {
		l.WarnContext(ctx, "review attempt ended without a report", "attempt", attempt, "task", rec.ID, "reason", reason)
		if attempt >= MaxAttempts {
			o := NoReport(fmt.Sprintf("Attempt %d of %d ended without a report: %s", attempt, MaxAttempts, reason), e.footer(b, pr, rec.ID))
			if in != nil && in.semgrep != nil {
				st, _ := LoadState(run.Text)
				st.Semgrep = in.semgrep.stamp()
				st.Semgrep.NoReport = true
				if b.ItsaplanProject != "" {
					// Preserve the exhausted attempt bound while both evidence
					// snapshots stay unchanged. NoReport still forbids a pass.
					st.IssueDigest = issueDigest(in.issues)
				}
				o.Output.Text = encodeState(st)
				if in.semgrep.Unavailable != "" {
					o.Output.Summary += "\n\n**medium** `semgrep:unverified`: " + in.semgrep.Unavailable + "."
				}
			}
			e.finish(ctx, b, pr, run, o, l)
			return true
		}
		e.m.Retries.WithLabelValues(b.Name).Inc()
		var gate *Gate
		if in != nil {
			gate = in.gate
		}
		note := fmt.Sprintf("Attempt %d ended without a report: %s Attempt %d follows.", attempt, reason, attempt+1)
		attempt++
		rec, msg, sends = recorded{Attempt: attempt}, nil, 0
		e.recordProgress(ctx, b, run, gate, rec, note, l)
		e.sleep(ctx, e.cfg.RetryBackoff)
		began = e.cfg.Now()
		return false
	}
	for {
		if ctx.Err() != nil {
			e.stop(ctx, b, pr, run, rec.ID, l)
			return
		}
		late := !e.cfg.Now().Before(began.Add(e.cfg.Timeout))
		if in == nil {
			got, err := e.gather(ctx, b, pr, l)
			switch {
			case err == nil:
				in = got
				sameSnapshot := false
				if st, ok := LoadState(run.Text); ok {
					in.goal = st.Goal // goalCheck reuses it for the same evidence.
					if st.Incomplete {
						// A complete replay restores the state; otherwise
						// it stays incomplete and keeps the check blocked.
						st, _ = e.recoverState(ctx, b, pr, run, st)
					}
					if st.Incomplete || st.Blocking != nil || (st.Gate != nil && len(st.Gate.Findings) > 0) {
						// previousState skips the same head; retain its report too.
						in.prior, in.priorHead, in.priorUnknown = st, pr.HeadSHA, ""
						if st.Incomplete {
							in.priorUnknown = "the same head's complete review history could not be recovered"
						}
					}
					if st.Gate != nil {
						if in.gate == nil {
							in.gate = &Gate{}
						}
						in.gate.inherit(st.Gate)
						for _, p := range st.Gate.Paths {
							if len(in.gate.Paths) < maxGatePaths && !slices.Contains(in.gate.Paths, p) {
								in.gate.Paths = append(in.gate.Paths, p)
							}
						}
						in.gate.ObservedLabels = st.Gate.ObservedLabels
						if !st.Gate.ApprovalAfter.Before(in.gate.ApprovalAfter) {
							in.gate.ApprovalAfter, in.gate.ApprovalSnapshot, in.gate.ApprovalNonce = st.Gate.ApprovalAfter, st.Gate.ApprovalSnapshot, st.Gate.ApprovalNonce
						}
						in.gate.LabelHistory = st.Gate.LabelHistory
						sameSnapshot = true
					}
				}
				if !sameSnapshot && in.gate != nil && len(in.gate.Labels) > 0 {
					// An unknown label baseline needs fresh approval.
					in.gate.resetApproval(e.cfg.Now())
				}
				// Rebind inherited prior-head recovery to this PR.
				in.gate, _ = e.recoveryGate(b, pr, in.gate)
				in.gate, _ = e.retainGate(b, pr, in.gate, false)
			case rec.ID == "" && e.waitForQuota(ctx, b, run, rec, l):
				// Unreadable PR data must not hold a worker or spend an
				// attempt while the grant is also blocked.
				return
			case late:
				if next("the reviewer could not read the PR from GitHub.") {
					return
				}
				continue
			default:
				e.sleep(ctx, e.cfg.Reissue)
				continue
			}
			if rec.ID == "" && e.requestSplit(ctx, b, pr, run, in.size, l) {
				return
			}
		}
		if b.ItsaplanProject != "" && len(in.issues.Pending) > 0 {
			if rec.ID != "" {
				e.cancelTask(ctx, b, rec.ID, l)
			}
			st := in.prior
			st.Gate = in.gate
			e.holdIssues(ctx, b, pr, run, st, in.issues.Pending)
			return // Release the slot; every poll retries without another push.
		}
		if rec.ID != "" && b.ItsaplanProject != "" && rec.Issues != issueDigest(in.issues) {
			// The task was sent for other evidence: it predates issue reads,
			// or the issue changed while the service was down. Its check
			// binds to the evidence the task was sent with, so start again.
			_, _ = e.tasks.CancelTask(ctx, b.Agent, rec.ID)
			if next("the recorded task did not review the current issue evidence.") {
				return
			}
			continue
		}
		if e.cfg.Semgrep != nil && in.semgrep == nil {
			start := run.StartedAt
			if start.IsZero() {
				start = began
			}
			in.semgrep = e.semgrepEvidence(ctx, b, pr, start.Add(e.cfg.Semgrep.Wait))
			if ctx.Err() != nil {
				continue
			}
			if rec.ID != "" && rec.Semgrep != in.semgrep.stamp().Digest {
				e.cancelTask(ctx, b, rec.ID, l)
				if next("Semgrep evidence changed or was not recorded before this task; review again.") {
					return
				}
				continue
			}
		}
		if rec.ID == "" {
			if e.waitForQuota(ctx, b, run, rec, l) {
				return
			}
			rec.QuotaSince = time.Time{}
			if msg == nil {
				m, err := e.message(ctx, b, pr, run, in, attempt, refused, l)
				switch {
				case err == nil:
					msg, sends = m, 0
					if rec.QuotaRetry > 0 {
						msg.MessageID += fmt.Sprintf("/quota-%d", rec.QuotaRetry)
					}
				case late:
					if next("the reviewer could not read the rules or the PR from GitHub.") {
						return
					}
					continue
				default:
					e.sleep(ctx, e.cfg.Reissue)
					continue
				}
			}
			if sends > e.cfg.MaxSends {
				if next(fmt.Sprintf("kagent did not take the review task in %d sends.", sends)) {
					return
				}
				continue
			}
			if wait := e.cfg.DispatchWindow - e.cfg.Now().Sub(lastSend); sends == e.cfg.MaxSends && wait > 0 {
				// The final send waits until nothing sent earlier can still
				// start, so it finds any task kagent took meanwhile.
				e.sleep(ctx, wait)
				continue
			}
			// Rules and session reads can take time. Check again at dispatch.
			if e.waitForQuota(ctx, b, run, rec, l) {
				return
			}
			t, err := e.tasks.Send(ctx, b.Agent, *msg)
			sends++
			lastSend = e.cfg.Now()
			switch {
			case err == nil:
				rec = recorded{Attempt: attempt, ID: t.ID, Sent: lastSend, QuotaRetry: rec.QuotaRetry}
				if b.ItsaplanProject != "" {
					rec.Issues = issueDigest(in.issues)
				}
				if in.semgrep != nil {
					rec.Semgrep = in.semgrep.stamp().Digest
				}
				e.m.ReviewSessions.WithLabelValues(b.Name, in.plan.path(), in.plan.reason).Inc()
				l.InfoContext(ctx, "review task sent", "message_id", msg.MessageID, "task", t.ID, "attempt", attempt, "sends", sends,
					"path", in.plan.path(), "reason", in.plan.reason, "context_id", t.ContextID)
				e.recordProgress(ctx, b, run, in.gate, rec, "", l)
				e.nameSession(ctx, b, pr, t.ContextID, l)
			case in.plan.resume != nil && (errors.Is(err, kagent.ErrRejected) || errors.Is(err, kagent.ErrTaskNotFound)):
				// The session went away or will not take the continuation.
				e.kagentError(ctx, l, b, "send", err)
				l.WarnContext(ctx, "kagent refused the review in its recorded session; starting a fresh one", "context_id", in.plan.resume.ContextID)
				refused, msg = true, nil
			case errors.Is(err, kagent.ErrRejected):
				e.kagentError(ctx, l, b, "send", err)
				if next("kagent rejected the review task.") {
					return
				}
			default:
				e.kagentError(ctx, l, b, "send", err)
				e.sleep(ctx, e.cfg.Reissue)
			}
			continue
		}
		t, err := e.tasks.GetTask(ctx, b.Agent, rec.ID)
		switch {
		case err == nil && t.Status.State == kagent.StateFailed &&
			dispatch.Classify(t.Status.State, dispatch.StatusText(t)) == dispatch.ClassQuota:
			// The provider refused this task. Keep the attempt and give its
			// eventual replacement a new message ID, persisted before sending.
			rec = recorded{Attempt: attempt, QuotaRetry: rec.QuotaRetry + 1, QuotaSince: e.cfg.Now()}
			e.recordProgress(ctx, b, run, in.gate, rec, "The review task reached the provider usage limit. It will resume after a newer usable broker reading.", l)
			return
		case err == nil && kagent.Quiescent(t.Status.State):
			current, err := e.gh.PullRequestQueue(ctx, b.repo(), pr.Number)
			if err != nil {
				e.ghError(ctx, l, b, "pull_request", err)
				return // Keep the completed task attached; the next poll retries this read.
			}
			if current.State != "open" || current.Draft || current.BaseRef != b.BaseBranch || current.HeadSHA != pr.HeadSHA {
				e.complete(ctx, b, run, Outcome{Conclusion: ConclusionCancelled, Output: github.CheckRunOutput{
					Title: "PR changed during review", Summary: "The PR is no longer open and ready on this head and base branch.",
				}}, l)
				return
			}
			currentPR := pr
			currentPR.Labels = current.Labels
			prs, complete, err := e.gh.ListPullRequests(ctx, b.repo(), b.BaseBranch)
			if err != nil {
				e.ghError(ctx, l, b, "list_pulls", err)
				return
			}
			counts, labels := readyHeads(b, prs)
			if !complete || counts[pr.HeadSHA] > 1 {
				for _, p := range prs {
					if p.HeadSHA == pr.HeadSHA {
						e.retainGate(b, p, &Gate{SharedHead: true, Labels: labels[pr.HeadSHA]}, false)
					}
				}
				e.finish(ctx, b, pr, run, sharedHeadOutcome(run, labels[pr.HeadSHA], e.cfg.Now()), l)
				return
			}
			listed := slices.IndexFunc(prs, func(p github.PullRequest) bool {
				return p.Number == pr.Number && p.HeadSHA == pr.HeadSHA && p.BaseRef == b.BaseBranch && !p.Draft
			})
			if listed < 0 {
				e.complete(ctx, b, run, Outcome{Conclusion: ConclusionCancelled, Output: github.CheckRunOutput{
					Title: "PR changed during review", Summary: "The PR is no longer open and ready on this head and base branch.",
				}}, l)
				return
			}
			currentPR = prs[listed] // Body/branch references can change on the same head.
			if in.gate == nil {
				in.gate = &Gate{ObservedLabels: []string{}}
			}
			before := in.gate.ApprovalNonce
			in.gate.observeLabels(gateLabels(b, currentPR.Labels), e.cfg.Now())
			events, err := e.gh.PullRequestLabelEvents(ctx, b.repo(), pr.Number)
			if err != nil {
				e.ghError(ctx, l, b, "label_events", err)
				in.gate, _ = e.retainGate(b, pr, in.gate, before != in.gate.ApprovalNonce)
				e.recordProgress(ctx, b, run, in.gate, rec, "Waiting for complete hold history.", l)
				return // Reattach the completed task once durable hold history is readable.
			}
			in.gate.observeLabelEvents(b, events)
			in.gate, _ = e.retainGate(b, pr, in.gate, before != in.gate.ApprovalNonce)
			built := in.compared // This process built the task's prompt.
			since, err := e.interdiff(ctx, b, currentPR, in, l)
			if err != nil {
				// The prompt already lowered late findings, so a review
				// assessed without the interdiff could pass on less than
				// a full review: wait for it instead.
				e.recordProgress(ctx, b, run, in.gate, rec, "Waiting for the changes since the previous review.", l)
				return // Reattach the completed task once the interdiff is readable.
			}
			if !built && since == nil && in.priorHead != "" && in.priorHead != currentPR.HeadSHA && !in.prior.Incomplete {
				// A task prompted in another process may have had an
				// interdiff GitHub no longer has; its reply may hold
				// lowered late findings, so review the whole change again.
				if next("GitHub no longer has the earlier reviewed head " + short(in.priorHead) + ", so the whole change is reviewed again.") {
					return
				}
				continue
			}
			res := e.outcome(ctx, b, currentPR, t, in, l)
			if ctx.Err() != nil {
				continue // stop ends the review.
			}
			if res.reason != "" {
				if next(res.reason) {
					return
				}
				continue
			}
			if b.ItsaplanProject != "" {
				fresh := e.issueContext(ctx, b, currentPR, in.files)
				if len(fresh.Pending) > 0 || issueDigest(fresh) != rec.Issues {
					// The completed report only covers the old snapshot. Preserve
					// its findings and human holds, then review the same head anew.
					st, _ := LoadState(res.o.Output.Text)
					e.holdIssues(ctx, b, currentPR, run, st, fresh.Pending)
					return
				}
			}
			if e.finish(ctx, b, currentPR, run, res.o, l) {
				e.observe(ctx, b, res.r, res.a, l)
				e.observeSession(ctx, b, t, res.session, l)
			}
			return
		case errors.Is(err, kagent.ErrTaskNotFound):
			// Expired or never seen: the attempt's message finds or opens it.
			l.WarnContext(ctx, "review task not found; sending its message again", "task", rec.ID)
			e.m.KagentErrors.WithLabelValues(b.Name, "get_task").Inc()
			rec = recorded{Attempt: attempt, QuotaRetry: rec.QuotaRetry}
			e.sleep(ctx, e.cfg.TaskPoll)
			continue
		case err != nil:
			e.kagentError(ctx, l, b, "get_task", err)
		}
		// The task is read before its time runs out, so one that finished
		// in time is never timed out.
		if !e.cfg.Now().Before(rec.Sent.Add(e.cfg.Timeout)) {
			e.cancelTask(ctx, b, rec.ID, l)
			if next(fmt.Sprintf("its task did not finish within %s.", e.cfg.Timeout)) {
				return
			}
			continue
		}
		e.sleep(ctx, e.cfg.TaskPoll)
	}
}

func (e *Engine) sleep(ctx context.Context, d time.Duration) {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

// gather reads the PR's changed files and the previous round's state.
func (e *Engine) gather(ctx context.Context, b *Binding, pr github.PullRequest, l *slog.Logger) (*inputs, error) {
	files, err := e.gh.PullRequestFiles(ctx, b.repo(), pr.Number)
	if err != nil {
		e.ghError(ctx, l, b, "files", err)
		return nil, err
	}
	prior, head, unknown, err := e.previousState(ctx, b, pr)
	if err != nil {
		return nil, err
	}
	if unknown != "" {
		l.WarnContext(ctx, "previous review state is incomplete; retaining the hold and asking for a full review", "reason", unknown)
	}
	gate := humanGate(b, changedPaths(files), pr.Labels)
	if prior.Gate.held() {
		if gate == nil {
			gate = &Gate{ObservedLabels: gateLabels(b, pr.Labels)}
		}
		gate.inherit(prior.Gate)
	}
	return &inputs{
		issues: e.issueContext(ctx, b, pr, files),
		files:  files, size: measure(b, files), gate: gate,
		prior: prior, priorHead: head, priorUnknown: unknown,
	}, nil
}

// maxPreviousLookups bounds the earlier heads whose review check is read.
const maxPreviousLookups = 20

// previousState reads the State kept by the review of the newest earlier
// head of the PR that has one, and that head. One GraphQL read lists the
// heads that carry a completed review check, or may carry one past what it
// lists; only those are read. A review
// that ended without a report keeps none and is skipped. When the search
// stops at a bound (an older timeline than was read, or more reviewed heads
// than maxPreviousLookups), unknown says so instead of starting fresh.
func (e *Engine) previousState(ctx context.Context, b *Binding, pr github.PullRequest) (st State, head, unknown string, err error) {
	hist, err := e.gh.PullRequestHeads(ctx, b.repo(), pr.Number, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID})
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "pull_request_heads", err)
		return State{}, "", "", err
	}
	return e.previousStateFrom(ctx, b, pr, hist)
}

func (e *Engine) previousStateFrom(ctx context.Context, b *Binding, pr github.PullRequest, hist *github.HeadHistory) (st State, head, unknown string, err error) {
	// This PR's own reviews decide. Only a PR that has none on any earlier
	// head (a replacement PR) inherits the reviews of its shared commits,
	// so another PR's checkpoint never stops the walk before this PR's own
	// older history.
	truncated := func() (State, string, string, error) {
		return State{Incomplete: true}, "", "its timeline has more commit and push events than the reviewer reads", nil
	}
	for _, inherit := range []bool{false, true} {
		if inherit && hist.Truncated {
			// This PR's own review may lie beyond the visible timeline.
			return truncated()
		}
		st, head, unknown, found, err := e.previousStateWalk(ctx, b, pr, hist, inherit)
		if err != nil || found {
			return st, head, unknown, err
		}
	}
	return State{}, "", "", nil
}

func (e *Engine) previousStateWalk(ctx context.Context, b *Binding, pr github.PullRequest, hist *github.HeadHistory, inherit bool) (st State, head, unknown string, found bool, err error) {
	seen := map[string]bool{pr.HeadSHA: true}
	looked := 0
	for i := len(hist.Heads) - 1; i >= 0; i-- {
		h := hist.Heads[i]
		if seen[h.SHA] || !h.Checked {
			continue
		}
		seen[h.SHA] = true
		if looked == maxPreviousLookups {
			return State{Incomplete: true}, "", fmt.Sprintf("more than %d earlier heads carry a review check", maxPreviousLookups), true, nil
		}
		looked++
		run, st, lost, err := e.statefulReview(ctx, b, pr.Number, h.SHA, inherit)
		if err != nil {
			return State{}, "", "", true, err
		}
		if run == nil {
			if lost {
				return State{Incomplete: true}, "", "a review check of an earlier head keeps no readable review state", true, nil
			}
			continue
		}
		if st.Incomplete {
			recovered, rec := e.recoverState(ctx, b, pr, run, st)
			if rec == replayDone {
				return recovered, h.SHA, "", true, nil
			}
			// A gap or a failed read keeps the state incomplete; a later
			// poll retries the read.
			return recovered, h.SHA, "the earlier review state is incomplete; known findings still require accounting and missing history keeps the check blocked", true, nil
		}
		return st, h.SHA, "", true, nil
	}
	return State{}, "", "", false, nil
}

// statefulReview is the newest completed review check on sha that keeps
// review state. A later run that keeps none (a review that ended without a
// report, a split request, a cancelled attempt) does not hide it. lost says
// a newer completed run that is none of these keeps no readable state.
func (e *Engine) statefulReview(ctx context.Context, b *Binding, pr int, sha string, inherit bool) (run *github.CheckRun, st State, lost bool, err error) {
	runs, err := e.gh.ListCheckRuns(ctx, b.repo(), sha, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID, All: true})
	if err != nil {
		e.ghError(ctx, e.log.With("binding", b.Name, "repo", b.repo(), "sha", sha), b, "check_runs", err)
		return nil, State{}, false, err
	}
	sort.Slice(runs, func(i, j int) bool { return runs[i].ID > runs[j].ID })
	// This PR's own newest review is its state for the commit. With inherit
	// (a PR with no review of its own), every other review of the shared
	// commit is merged fail-closed: a newer clean review must not hide an
	// older incomplete or blocking one.
	var inherited *github.CheckRun
	var merged State
	for i := range runs {
		r := &runs[i]
		if r.Name != e.cfg.CheckName || r.AppID != e.cfg.AppID || r.HeadSHA != sha || r.Status != "completed" ||
			r.Title == "No review report" || r.Title == "Split this PR" || r.Conclusion == ConclusionCancelled {
			continue
		}
		if r.ExternalID != MessageID(b.repo(), pr, sha) {
			if !inherit {
				continue
			}
			st, ok := LoadState(r.Text)
			if !ok {
				return nil, State{}, true, nil
			}
			if inherited == nil {
				inherited, merged = r, st
				merged.Blocking = slices.Clone(st.Blocking)
				if merged.Gate != nil {
					g := *merged.Gate
					g.Findings = slices.Clone(g.Findings)
					merged.Gate = &g
				}
				continue
			}
			merged.Incomplete = merged.Incomplete || st.Incomplete
			merged.Overflow = merged.Overflow || st.Overflow
			merged.Blocking = mergeFindings(merged.Blocking, st.Blocking)
			if st.Gate != nil {
				if merged.Gate == nil {
					merged.Gate = &Gate{}
				}
				merged.Gate.Findings = mergeFindings(merged.Gate.Findings, st.Gate.Findings)
			}
			continue
		}
		if st, ok := LoadState(r.Text); ok {
			return r, st, false, nil
		}
		return nil, State{}, true, nil
	}
	if inherited != nil {
		return inherited, merged, false, nil
	}
	return nil, State{}, false, nil
}

// mergeFindings adds each finding of more to list, keeping the higher
// severity of a shared ID.
func mergeFindings(list, more []Finding) []Finding {
	for _, f := range more {
		if i := slices.IndexFunc(list, func(o Finding) bool { return o.ID == f.ID }); i < 0 {
			list = append(list, f)
		} else if rank(f.Severity) < rank(list[i].Severity) {
			list[i] = f
		}
	}
	return list
}

// decisionsChanged re-checks the decisions of Joe's that a completed run's
// review relied on (State.Decisions). unread is a failed read: the pass
// cannot be confirmed on this poll.
func (e *Engine) decisionsChanged(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (changed, unread bool) {
	if run == nil || run.Status != "completed" {
		return false, false
	}
	st, ok := LoadState(run.Text)
	if !ok || len(st.Decisions) == 0 {
		return false, false
	}
	comments, _, err := e.trustedComments(ctx, b, pr)
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "comments", err)
		return false, true
	}
	return !joeDecisions(b, pr, comments).Hold(st.Decisions), false
}

func machineComment(body string) bool {
	body = strings.TrimSpace(body)
	for _, marker := range config.DefaultMachineCommentMarkers {
		if strings.HasPrefix(body, marker) {
			return true
		}
	}
	return false
}

// joeDecisions are the trusted comments by a human approver: Joe's, not
// those of every login whose comments reach the prompt.
func joeDecisions(b *Binding, pr github.PullRequest, comments []github.Comment) Decisions {
	d := Decisions{Repo: b.repo(), Number: pr.Number}
	for _, c := range comments {
		if containsFold(b.HumanApprovers, c.Author) && !machineComment(c.Body) {
			d.Comments = append(d.Comments, c)
		}
	}
	return d
}

// replay is the outcome of a replay of earlier review history.
type replay int

const (
	// replayUnread: a GitHub or kagent read failed. The evidence can
	// still exist, so the caller keeps the state incomplete and retries.
	replayUnread replay = iota
	// replayGap: the history cannot be replayed (gap). A retry cannot
	// recover it.
	replayGap
	replayDone
)

// recoverState replays original reports back to a complete v2 checkpoint or
// the beginning of a complete PR timeline. It never infers resolution from a
// passing conclusion. Lossless legacy snapshots can reconstruct findings
// from expired tasks, but cannot prove their resolution. A failed read
// leaves the caller blocked until a later poll. Bounds, ambiguous
// identities, invalid reports and lost history (an expired task whose legacy
// record is lossy) are gaps: the state stays incomplete and blocked, and no
// approval clears it. A new PR is the way out.
func (e *Engine) recoverState(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, saved State) (State, replay) {
	var known []Finding        // Every readable earlier finding, for gap.
	overflow := saved.Overflow // Any earlier overflowed checkpoint on the walk.
	unread := func() (State, replay) { return saved, replayUnread }
	gap := func() (State, replay) { return carryKnown(saved, known, overflow), replayGap }
	hist, err := e.gh.PullRequestHeads(ctx, b.repo(), pr.Number, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID})
	if err != nil {
		return unread()
	}
	type originalReport struct {
		id       int64
		head     string
		reply    string
		legacy   bool
		stored   State
		snapshot bool
	}
	var reports []originalReport
	seenHeads, seenTasks := map[string]bool{}, map[string]int{}
	prior := State{Blocking: []Finding{}}
	started, anchored := false, false
	lastHead := ""
	looked := 0
heads:
	for i := len(hist.Heads) - 1; i >= 0; i-- {
		h := hist.Heads[i]
		if h.SHA == run.HeadSHA {
			started = true
		}
		if !started || (!h.Checked && h.SHA != run.HeadSHA) {
			continue
		}
		if seenHeads[h.SHA] {
			if h.SHA != lastHead {
				return gap() // A return to an earlier head has ambiguous ancestry.
			}
			continue
		}
		lastHead = h.SHA
		seenHeads[h.SHA] = true
		looked++
		if looked > maxPreviousLookups {
			return gap()
		}
		runs, err := e.gh.ListCheckRuns(ctx, b.repo(), h.SHA, github.CheckRunFilter{Name: e.cfg.CheckName, AppID: e.cfg.AppID, All: true})
		if err != nil {
			return unread()
		}
		if len(runs) == 0 {
			return unread() // A checked timeline head cannot disappear: an inconsistent read.
		}
		sort.Slice(runs, func(i, j int) bool { return runs[i].ID > runs[j].ID })
		needsReport, foundReport := false, false
		var orphans []Finding
		firstReport := len(reports)
		for _, old := range runs {
			if old.Name != e.cfg.CheckName || old.AppID != e.cfg.AppID || old.HeadSHA != h.SHA || old.Status != "completed" {
				continue
			}
			if old.ExternalID != MessageID(b.repo(), pr.Number, h.SHA) {
				return gap()
			}
			if old.Title == "No review report" || old.Title == "Split this PR" {
				continue // These service outcomes contain no new model report.
			}
			if corruptState(old.Text) {
				return gap() // Its review state cannot be read.
			}
			st, ok := LoadState(old.Text)
			if !ok || (!st.Incomplete && st.Blocking == nil) {
				continue // No completed report, only attempt/gate bookkeeping.
			}
			if ok && !st.Incomplete && st.Blocking != nil {
				prior, anchored = st, true
				break heads
			}
			known = append(known, st.Previous()...)
			overflow = overflow || st.Overflow
			needsReport = needsReport || ok
			taskID := e.sourceTask(b, &old, st)
			if taskID == "" {
				if st.Overflow || !st.LegacyTask {
					return gap() // Its dropped findings have no report to replay.
				}
				// A legacy gate replacement can refer to an older report on
				// this head. That report must cover every finding it keeps.
				orphans = append(orphans, st.Previous()...)
				continue
			}
			foundReport = true
			if index, seen := seenTasks[taskID]; seen {
				// A gate replacement references its original report. Its later
				// check ID must not reorder that report after another head.
				if old.ID < reports[index].id {
					reports[index].id = old.ID
					reports[index].head = old.HeadSHA
					reports[index].stored = st
					_, reports[index].snapshot = legacySnapshot(old.Text)
				}
				reports[index].legacy = reports[index].legacy || st.LegacyTask
				continue
			}
			if len(taskID) > 200 || len(reports) >= maxPreviousLookups {
				return gap()
			}
			task, err := e.tasks.GetTask(ctx, b.Agent, taskID)
			if err != nil && !errors.Is(err, kagent.ErrTaskNotFound) {
				return unread() // Only a missing task is expired history.
			}
			reply := ""
			if err == nil && task != nil && task.ID == taskID && (task.Status.State == kagent.StateCompleted || task.Status.State == kagent.StateInputRequired) {
				reply = ReplyText(task)
			}
			_, snapshot := legacySnapshot(old.Text)
			seenTasks[taskID] = len(reports)
			reports = append(reports, originalReport{id: old.ID, head: old.HeadSHA, reply: reply, legacy: st.LegacyTask, stored: st, snapshot: snapshot})
		}
		if needsReport && !foundReport {
			return gap()
		}
		covered := map[string]bool{}
		for _, r := range reports[firstReport:] {
			for _, f := range r.stored.Previous() {
				covered[f.ID] = true
			}
		}
		for _, f := range orphans {
			if !covered[f.ID] {
				return gap() // No report on this head can account for it.
			}
		}
	}
	if !started || len(reports) == 0 || (!anchored && hist.Truncated) {
		return gap()
	}
	sort.Slice(reports, func(i, j int) bool { return reports[i].id < reports[j].id })
	var currentIssues *IssueContext
	readIssues := false
	var decisions Decisions
	readDecisions := false
	goalIDs := regexp.MustCompile(`^` + regexp.QuoteMeta(b.ItsaplanProject) + `-[1-9][0-9]*:goal(:|$)`)
	for _, r := range reports {
		var report *Report
		if r.reply == "" {
			if !r.snapshot {
				// A task without a reply, or an expired task whose legacy
				// record is lossy, cannot be replayed.
				return gap()
			}
			// A snapshot can recover identities, but absence is not evidence
			// of a fix or acceptance. A later report must account for them.
			report = &Report{Findings: r.stored.Previous()}
		} else {
			var err error
			report, err = parseReport(r.reply, r.legacy)
			if err != nil {
				return gap()
			}
			// Task replies never contain the service's issue evidence. Keep
			// known service findings, including when the model claims a fix.
			issues := &IssueContext{}
			for _, f := range r.stored.Blocking {
				if strings.HasPrefix(f.ID, "itsaplan:unverified:") {
					issues.Findings = append(issues.Findings, f)
				}
			}
			applyIssueFindings(report, issues)
		}
		if b.ItsaplanProject != "" {
			key, goal := r.stored.GoalKey, r.stored.Goal
			if goal != nil && (goal.Head != r.head || goal.Digest != r.stored.IssueDigest) {
				return gap()
			}
			if key == "" && r.stored.IssueDigest != "" && !r.snapshot {
				// Legacy answers did not store their issue key. A lossless
				// snapshot can carry its known goals; a clipped one needs the
				// exact evidence digest to reconstruct any omitted identities.
				if !readIssues {
					files, err := e.gh.PullRequestFiles(ctx, b.repo(), pr.Number)
					if err != nil {
						return unread()
					}
					readIssues, currentIssues = true, e.issueContext(ctx, b, pr, files)
				}
				if currentIssues == nil || len(currentIssues.Pending) > 0 {
					return unread() // The issue evidence could not be read.
				}
				if issueDigest(currentIssues) != r.stored.IssueDigest {
					return gap()
				}
				key = goalKey(currentIssues)
			}
			if key != "" && issueNumber(key, b.ItsaplanProject) == 0 {
				return gap()
			}
			// The code review cannot clear goal findings, including during
			// replay. Only a bound goal answer can resolve them. Re-add known
			// service findings when an older snapshot has no bound answer.
			applyGoal(report, prior.Previous(), b.ItsaplanProject, key, goal)
			known := &IssueContext{}
			for _, f := range r.stored.Blocking {
				if goalIDs.MatchString(f.ID) {
					known.Findings = append(known.Findings, f)
				}
			}
			applyIssueFindings(report, known)
		}
		// Revalidate accepted highs during replay too. Expired-task snapshots
		// retain their decision stamps; original replies reconstruct them
		// under the full finding IDs, including formerly clipped v1 stamps.
		carried := slices.Clone(prior.Decisions)
		if r.reply == "" {
			for _, d := range r.stored.Decisions {
				if !slices.Contains(carried, d) {
					carried = append(carried, d)
				}
			}
		}
		needsDecisions := len(carried) > 0 || slices.ContainsFunc(report.Previous, func(p Previous) bool {
			return strings.HasPrefix(strings.ToLower(strings.TrimSpace(p.Note)), "decision:")
		})
		if needsDecisions && !readDecisions {
			comments, _, err := e.trustedComments(ctx, b, pr)
			if err != nil {
				return unread()
			}
			decisions, readDecisions = joeDecisions(b, pr, comments), true
		}
		decisions.Carried = carried
		// A historical account cannot accept the edited text of a comment.
		// Match the saved hash as well as the current author and link. v1
		// stamped a clipped ID; the original report supplies its full ID.
		for i, p := range report.Previous {
			if p.Status != StatusJustified || withdrawn(p.Note) {
				continue
			}
			high := func(f Finding) bool { return f.ID == p.ID && f.Severity == SeverityHigh }
			if !slices.ContainsFunc(prior.Previous(), high) && !slices.ContainsFunc(report.Findings, high) {
				continue
			}
			stamp := decisions.decided(p.Note, p.ID, SeverityHigh)
			if stamp != nil && !slices.ContainsFunc(r.stored.Decisions, func(old DecisionStamp) bool {
				identity := old.Finding == p.ID || r.legacy && old.Finding == clip(p.ID, maxTitle)
				return identity && old.ID == stamp.ID && old.Hash == stamp.Hash
			}) {
				report.Previous[i].Status = StatusOpen
			}
		}
		a := AssessDecided(report, prior.Previous(), decisions)
		a.Earlier, a.EarlierPartial = decisions.Revalidate(prior.Cleared), true // Recovery cannot certify the complete cleared record.
		prior = stateOf(a, report, &Gate{Findings: a.Gated})
	}
	// Keep the current gate's approval baseline and sticky service holds.
	// Its model findings come from the complete replay, never the lossy list.
	gate := &Gate{}
	if saved.Gate != nil {
		*gate = *saved.Gate
	}
	gate.Findings = nil
	if prior.Gate != nil {
		gate.Findings = prior.Gate.Findings
	}
	prior.Gate, prior.TaskID, prior.LegacyTask = gate, saved.TaskID, saved.LegacyTask
	prior.IssueDigest, prior.IssuesPending = saved.IssueDigest, saved.IssuesPending
	prior.Goal, prior.GoalKey = saved.Goal, saved.GoalKey
	prior.Semgrep = saved.Semgrep
	if b.ItsaplanProject != "" && issueReader(b, pr) {
		// Replay cannot reconstruct service findings omitted by legacy caps
		// or overflow. Recheck current evidence in a fresh review before pass.
		prior.IssuesPending = true
	}
	return prior, replayDone
}

// carryKnown is the state of a head whose review history has a gap. It
// stays incomplete: no review can show that a blocker it never saw is
// resolved. It carries every readable earlier finding, from older legacy
// snapshots too, unless a later report on this head already had them as its
// prior (saved is no legacy report).
func carryKnown(saved State, known []Finding, overflow bool) State {
	st := saved
	st.Incomplete = true
	st.Overflow = st.Overflow || overflow
	if !saved.LegacyTask {
		return st
	}
	gate := &Gate{}
	if saved.Gate != nil {
		*gate = *saved.Gate
	}
	gate.Findings = slices.Clone(gate.Findings)
	st.Blocking = slices.Clone(st.Blocking)
	if st.Blocking == nil {
		st.Blocking = []Finding{}
	}
	for _, f := range known {
		list := &st.Blocking
		if isGate(f) {
			list = &gate.Findings
		}
		if i := slices.IndexFunc(*list, func(o Finding) bool { return o.ID == f.ID }); i < 0 {
			*list = append(*list, f)
		} else if rank(f.Severity) < rank((*list)[i].Severity) {
			(*list)[i] = f
		}
	}
	if gate.Needed() || saved.Gate != nil {
		st.Gate = gate
	}
	return st
}

// gateFindings counts a gate's human-gate findings.
func gateFindings(g *Gate) int {
	if g == nil {
		return 0
	}
	return len(g.Findings)
}

func (e *Engine) sourceTask(b *Binding, run *github.CheckRun, st State) string {
	if st.TaskID != "" {
		return st.TaskID
	}
	if original, ok := LoadState(run.Text); ok && original.TaskID != "" {
		return original.TaskID
	}
	if st.Incomplete && !st.LegacyTask {
		return "" // A v2 overflow without its task reference cannot be replayed.
	}
	// Legacy checks have no structured reference. Only the service footer
	// at the end (optionally followed by enqueue history) supplies one.
	prefix := fmt.Sprintf("Reviewed %s by the kagent agent `%s/%s` as task `", short(run.HeadSHA), b.Agent.Namespace, b.Agent.Name)
	if at := strings.LastIndex(run.Summary, prefix); at >= 0 {
		id, _, _ := strings.Cut(run.Summary[at+len(prefix):], "`")
		footer := e.footer(b, github.PullRequest{HeadSHA: run.HeadSHA}, id)
		if tail, ok := strings.CutPrefix(run.Summary[at:], footer); ok {
			tail = strings.TrimSpace(tail)
			if tail == "" || strings.HasPrefix(tail, enqueuedMarker) {
				return id
			}
		}
	}
	return ""
}

// trustedComments are the PR's review bodies and comments from trusted
// logins; dropped counts the others.
func (e *Engine) trustedComments(ctx context.Context, b *Binding, pr github.PullRequest) (out []github.Comment, dropped int, err error) {
	reviews, err := e.gh.PullRequestReviews(ctx, b.repo(), pr.Number)
	if err != nil {
		return nil, 0, err
	}
	comments, err := e.gh.PullRequestComments(ctx, b.repo(), pr.Number)
	if err != nil {
		return nil, 0, err
	}
	for _, r := range reviews {
		comments = append(comments, github.Comment{Author: r.Author, Body: r.Body})
	}
	for _, c := range comments {
		switch {
		case machineComment(c.Body):
			dropped++
		case strings.TrimSpace(c.Body) == "", e.ownComment(c), e.cfg.Semgrep != nil && strings.EqualFold(c.Author, semgrepAuthor):
		case containsFold(b.TrustedCommenters, c.Author):
			out = append(out, c)
		default:
			dropped++
		}
	}
	if len(out) > maxComments {
		out = out[len(out)-maxComments:]
	}
	return out, dropped, nil
}

// message builds an attempt's review task. It is built once per attempt:
// kagent rejects a message ID sent again with other content. A message that
// resumes the previous head's session names it and adds "/resume" to its
// ID; any other names no session, so kagent opens a fresh one for it.
func (e *Engine) message(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, in *inputs, attempt int, refused bool, l *slog.Logger) (*kagent.Message, error) {
	repo := b.repo()
	rules, err := LoadRules(ctx, e.gh, repo, b.BaseBranch, changedPaths(in.files), b.ContextDocs)
	if err != nil {
		e.ghError(ctx, l, b, "rules", err)
		return nil, err
	}
	comments, dropped, err := e.trustedComments(ctx, b, pr)
	if err != nil {
		e.ghError(ctx, l, b, "comments", err)
		return nil, err
	}
	in.comments, in.commentsRead = comments, true
	cited := e.citedEvidence(ctx, b, pr, l)
	interdiff, err := e.interdiff(ctx, b, pr, in, l)
	if err != nil {
		return nil, err
	}
	scope, err := e.laterScope(ctx, b, pr, in, l)
	if err != nil {
		return nil, err
	}
	if !in.prior.ClearedComplete() {
		scope = nil // A fixed finding missing from the record could be lowered.
	}
	commits := e.commitMessages(ctx, b, pr, in.priorHead, l)
	plan := sessionPlan{rules: rulesDigest(rules)}
	plan.resume, plan.reason = e.resumable(ctx, b, pr, in, attempt, refused, l)
	if plan.reason == reasonUnsettled {
		return nil, errUnsettledTurn
	}
	text := Prompt(PromptInput{
		Repo: repo, Number: pr.Number, URL: pr.URL, Base: b.BaseBranch, SHA: pr.HeadSHA,
		Title: pr.Title, Body: pr.Body, Author: pr.Author, Files: in.files, Rules: rules,
		Previous: in.prior.Previous(), Cleared: joeDecisions(b, pr, comments).Revalidate(in.prior.Cleared), PreviousHead: in.priorHead, Interdiff: interdiff, Scope: scope, PreviousUnknown: in.priorUnknown,
		Resumed: plan.resume != nil, RulesHeld: plan.resume != nil && plan.rules != "" && plan.resume.Rules == plan.rules,
		Comments: comments, DroppedComments: dropped, Cited: cited, Commits: &commits, Gate: in.gate, Attempt: attempt,
		CheckName: e.cfg.CheckName, Semgrep: in.semgrep,
	})
	id, contextID := AttemptMessageID(repo, pr.Number, pr.HeadSHA, run.ID, attempt), ""
	if plan.resume != nil {
		id, contextID = id+"/resume", plan.resume.ContextID
	}
	in.plan = plan
	msg := &kagent.Message{
		MessageID: id,
		ContextID: contextID,
		Parts:     []kagent.Part{{Text: text}},
		Metadata: map[string]any{
			"factory.jomcgi.dev/repository":   repo,
			"factory.jomcgi.dev/pull-request": pr.Number,
			"factory.jomcgi.dev/head":         pr.HeadSHA,
		},
	}
	l.InfoContext(ctx, "review task built", "message_id", msg.MessageID, "attempt", attempt,
		"rules", rules.Paths(), "rules_commit", rules.Commit, "previous_head", in.priorHead, "previous_unknown", in.priorUnknown,
		"previous_findings", len(in.prior.Previous()), "trusted_comments", len(comments), "dropped_comments", dropped,
		"cited", len(cited), "commits", len(commits.List), "commits_since", commits.Since, "commits_unread", commits.Unread,
		"path", plan.path(), "reason", plan.reason, "context_id", contextID, "prompt_bytes", len(text))
	return msg, nil
}

// interdiff is what changed since the previous reviewed head, read once per
// review: nil for a first review, a same-head review, or a head GitHub no
// longer has.
func (e *Engine) interdiff(ctx context.Context, b *Binding, pr github.PullRequest, in *inputs, l *slog.Logger) (*github.CommitComparison, error) {
	if in.prior.Incomplete {
		in.since, in.compared = nil, true
		return nil, nil
	}
	if in.compared || in.priorHead == "" || in.priorHead == pr.HeadSHA {
		return in.since, nil
	}
	d, err := e.gh.CompareCommits(ctx, b.repo(), in.priorHead, pr.HeadSHA)
	if err != nil && !errors.Is(err, github.ErrNotFound) {
		e.ghError(ctx, l, b, "compare", err)
		return nil, err
	}
	in.since, in.compared = d, true
	return d, nil
}

// laterScope is the scope a later head's new findings are held to, read
// once per review: the hunks changed since the previous reviewed head, or
// after a rebase the files whose own diff changed (RebasedScope). It is nil
// for a first review, a missing or truncated interdiff, or a rebase whose
// earlier diff the service cannot read; the whole change is then reviewed
// again at full severity.
func (e *Engine) laterScope(ctx context.Context, b *Binding, pr github.PullRequest, in *inputs, l *slog.Logger) (*Scope, error) {
	if in.scoped {
		return in.scope, nil
	}
	d, err := e.interdiff(ctx, b, pr, in, l)
	if err != nil {
		return nil, err
	}
	s := LaterScope(in.priorHead, d)
	if s == nil && d != nil && d.Status == "diverged" && in.priorHead != "" {
		before, err := e.gh.CompareCommits(ctx, b.repo(), b.BaseBranch, in.priorHead)
		switch {
		case err != nil && !errors.Is(err, github.ErrNotFound):
			e.ghError(ctx, l, b, "compare", err)
			return nil, err
		case err == nil && before != nil:
			s = RebasedScope(in.priorHead, before.Files, in.files, !before.Truncated && len(in.files) < github.MaxFilePages*100)
		}
	}
	in.scope, in.scoped = s, true
	return s, nil
}

// recordProgress writes the attempt, and its task once sent, on the
// in-progress check run, so a restart reads the task instead of sending a
// message whose PR text may have changed.
func (e *Engine) recordProgress(ctx context.Context, b *Binding, run *github.CheckRun, gate *Gate, rec recorded, note string, l *slog.Logger) {
	st, _ := LoadState(run.Text)
	if gate == nil {
		gate = st.Gate
	}
	if gate != nil {
		observed := *gate
		if st.Gate != nil {
			observed.Findings = st.Gate.Findings
		}
		st.Gate = &observed
	}
	run.Text = encodeState(st)
	title := "Review in progress"
	if !rec.QuotaSince.IsZero() {
		title = "Waiting for Codex quota"
	}
	_, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, github.CheckRunWrite{
		ExternalID: run.ExternalID,
		Output:     &github.CheckRunOutput{Title: title, Summary: keepEnqueueHistory(progressSummary(b, gate, rec, note), run), Text: run.Text},
	})
	if err != nil {
		e.ghError(ctx, l, b, "update_check", err)
	}
}

func (e *Engine) footer(b *Binding, pr github.PullRequest, taskID string) string {
	s := fmt.Sprintf("Reviewed %s by the kagent agent `%s/%s`", short(pr.HeadSHA), b.Agent.Namespace, b.Agent.Name)
	if taskID != "" {
		s += fmt.Sprintf(" as task `%s`", taskID)
	}
	return s + ". The reviewer service decides the conclusion from the findings' severities."
}

// result is how a quiescent task ends its review: an outcome with the
// report and assessment behind it, or the reason it gave no report.
type result struct {
	o       Outcome
	r       *Report
	a       *Assessment
	session *SessionRef
	reason  string
}

// outcome reads a quiescent task. Only a parsed report can pass, and a
// gated head passes only once its human approved it.
func (e *Engine) outcome(ctx context.Context, b *Binding, pr github.PullRequest, task *kagent.Task, in *inputs, l *slog.Logger) result {
	footer := e.footer(b, pr, task.ID)
	switch task.Status.State {
	case kagent.StateCompleted, kagent.StateInputRequired:
		reply := ReplyText(task)
		r, err := ParseReport(reply)
		if err != nil {
			l.WarnContext(ctx, "the review reply has no usable report", "task", task.ID, "error", err, "reply_bytes", len(reply))
			return result{reason: "the reviewer's reply had no usable findings report: " + oneLine(err.Error(), maxTitle) + "."}
		}
		// Only a usable code review starts the goal check, which costs a
		// second model call.
		key := goalKey(in.issues)
		if key == "" {
			in.goal = nil // The State keeps no answer for another issue.
		} else {
			in.goal = e.goalCheck(ctx, b, pr, in, r, l)
		}
		prior := in.prior.Previous()
		if _, err := e.interdiff(ctx, b, pr, in, l); err != nil {
			// review reads the interdiff before outcome; never assess without it.
			return result{reason: "the reviewer could not read the changes since the previous review: " + oneLine(err.Error(), maxTitle) + "."}
		}
		scope, err := e.laterScope(ctx, b, pr, in, l)
		if err != nil {
			return result{reason: "the reviewer could not read the changes since the previous review: " + oneLine(err.Error(), maxTitle) + "."}
		}
		if in.prior.Incomplete || !in.prior.ClearedComplete() {
			scope = nil // A fixed finding missing from the record could be lowered.
		}
		r = scope.Lower(r, prior, in.prior.Cleared)
		// The service's own evidence findings are added after lowering, so
		// none of them is ever lowered.
		applyIssueFindings(r, in.issues)
		if b.ItsaplanProject != "" {
			applyGoal(r, prior, b.ItsaplanProject, key, in.goal)
		}
		// Joe's decisions come from the trusted comments the prompt showed;
		// a finding justified by one counts only when its note links it
		// (Decisions.decided). After a restart the task's message was not
		// built here, so read them again. A failed read assesses nothing:
		// the attempt ends without a report and is retried.
		comments := in.comments
		if !in.commentsRead {
			var err error
			if comments, _, err = e.trustedComments(ctx, b, pr); err != nil {
				e.ghError(ctx, l, b, "comments", err)
				// Fixed text: GitHub's error can carry response text, and the
				// reason can reach the PR comment. ghError logged it.
				return result{reason: "the reviewer could not read the PR's trusted comments after a restart."}
			}
		}
		decisions := joeDecisions(b, pr, comments)
		decisions.Carried = in.prior.Decisions
		a := AssessDecided(r, prior, decisions)
		a.Earlier, a.EarlierPartial = decisions.Revalidate(in.prior.Cleared), !in.prior.ClearedComplete()
		a.Later = in.priorHead != "" && in.priorHead != pr.HeadSHA
		for _, f := range a.Deferred {
			if f.Lowered != "" {
				l.InfoContext(ctx, "lowered a new finding outside the interdiff", "id", f.ID, "severity", f.Lowered)
			}
		}
		a.Incomplete, a.Overflow = in.prior.Incomplete, in.prior.Overflow
		a.TaskID = task.ID
		// Include the binding before Render decides whether the complete
		// state fits. The task's recorded digest was checked against this input.
		a.IssueDigest, a.Goal = issueDigest(in.issues), in.goal
		a.GoalKey = key
		gate := &Gate{ObservedLabels: []string{}}
		if in.gate != nil {
			*gate = *in.gate
		}
		gate.observeLabels(gateLabels(b, pr.Labels), e.cfg.Now())
		for _, f := range a.Gated {
			gate.Findings = append(gate.Findings, storable(f))
		}
		if !gate.Needed() {
			gate = nil
		} else {
			if gate.Approver, err = e.approval(ctx, b, pr, gate); err != nil {
				l.WarnContext(ctx, "could not verify the PR's approval; the human gate stays closed", "error", err)
			}
			if gate.approved() {
				a.Approved, a.Approver = gate.Findings, gate.Approver
				if gate.Authorship != "" {
					a.Approver = pr.Author
				}
			} else {
				if gate.held() {
					a.Blocking = append(a.Blocking, Blocked{Finding: gateFinding(b, gate)})
				}
				for _, f := range gate.Findings {
					a.Blocking = append(a.Blocking, Blocked{Finding: f})
				}
				sortBlocked(a.Blocking)
			}
		}
		var semgrepState *State
		if in.semgrep != nil {
			// The goal check can run another model task after the code review.
			// Check scan freshness after both tasks, before saving the verdict.
			fresh := e.semgrepEvidence(ctx, b, pr, time.Time{})
			if *fresh.stamp() != *in.semgrep.stamp() {
				in.semgrep.Unavailable = "Semgrep evidence changed during the review; this head needs another review"
			}
			// Save the agent assessment before adding the service finding.
			// An agent finding with the same ID keeps its identity and severity.
			st := stateOf(a, r, gate)
			st.Semgrep = in.semgrep.stamp()
			semgrepState = &st
			enforceSemgrep(&a, in.semgrep)
		}
		a.Conclusion = ConclusionSuccess
		if len(a.Blocking) > 0 || a.Incomplete {
			a.Conclusion = ConclusionFailure
		}
		o := render(a, r, gate, footer, semgrepState)
		session := sessionAfter(b, pr, in, task)
		o.Output.Text = withSession(o.Output.Text, session)
		if stored, _ := LoadState(o.Output.Text); stored.Incomplete {
			a.Incomplete = true
		}
		o.Comment = reviewComment(a, r, b, gate)
		if in.priorUnknown != "" {
			// Render left maxPreface for this note, so the summary's tail
			// (the human-hold marker and footer) survives.
			o.Output.Summary = clip(fmt.Sprintf("The complete review state of an earlier head could not be recovered (%s). This head was reviewed in full; known unresolved findings still carry forward.\n\n",
				clip(in.priorUnknown, maxPreface-200)), maxPreface) + o.Output.Summary
		}
		return result{o: o, r: r, a: &a, session: session}
	}
	state := strings.ToLower(strings.TrimPrefix(task.Status.State, "TASK_STATE_"))
	status := ""
	if task.Status.Message != nil {
		status = clip(ReplyText(&kagent.Task{Status: task.Status}), 2000)
	}
	// The status text can be provider output; it stays in the logs.
	l.WarnContext(ctx, "the review task ended without a report", "task", task.ID, "state", state, "status", status)
	return result{reason: fmt.Sprintf("its task ended %s without a report; the reviewer's logs hold its status.", oneLine(state, 40))}
}

// approval is the configured human whose APPROVED review of the PR's head
// clears its gate, or empty. It also refreshes the transient authorship waiver.
func (e *Engine) approval(ctx context.Context, b *Binding, pr github.PullRequest, gate *Gate) (string, error) {
	gate.Authorship = ""
	reviews, err := e.gh.PullRequestReviews(ctx, b.repo(), pr.Number)
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "reviews", err)
		return "", err
	}
	// Capture once per hold, only after a complete review read.
	if (gate.RecoveryEpoch != "" || !gate.ApprovalAfter.IsZero()) && gate.ApprovalSnapshot == nil {
		gate.ApprovalSnapshot = snapshotReviews(reviews, gate.ApprovalAfter, gate.RecoveryEpoch != "")
	}
	acknowledged := map[int64]bool{}
	// Authenticate body, author and editor together, including third-party edits.
	latest := latestDecisions(reviews)
	for _, author := range b.HumanApprovers {
		r := latest[strings.ToLower(author)]
		if r.ID <= 0 || r.NodeID == "" || r.State != "APPROVED" || r.CommitSHA != pr.HeadSHA ||
			!containsFold(b.HumanApprovers, r.Author) || (gate.RecoveryEpoch == "" && !r.SubmittedAt.Equal(gate.ApprovalAfter.Truncate(time.Second))) ||
			!acknowledges(r.Body, gate.ApprovalNonce) || approvalAfter(r, gate.ApprovalAfter, gate.ApprovalSnapshot, false) {
			continue
		}
		proof, err := e.gh.PullRequestReviewAcknowledgement(ctx, b.repo(), pr.Number, r.NodeID)
		if err != nil {
			e.ghError(ctx, e.prLog(b, pr), b, "review_acknowledgement", err)
			continue
		}
		if proof.State == "APPROVED" && proof.CommitSHA == pr.HeadSHA && strings.EqualFold(proof.Author, r.Author) &&
			((proof.Editor == "" && proof.LastEditedAt == nil) || (proof.LastEditedAt != nil && strings.EqualFold(proof.Editor, r.Author))) &&
			acknowledges(proof.Body, gate.ApprovalNonce) {
			acknowledged[r.ID] = true
		}
	}
	approver := approvedBy(b, reviews, pr.HeadSHA, gate.ApprovalAfter, gate.ApprovalSnapshot, acknowledged)
	if approver == "" && !gate.reviewRequired() {
		gate.Authorship, err = e.authorshipApproval(ctx, b, pr)
		if err != nil {
			e.ghError(ctx, e.prLog(b, pr), b, "authorship_approval", err)
		}
	}
	return approver, err
}

// rule is a finding ID's rule when REVIEW.md or the configuration names it:
// the first rule segment that does, without its symbol suffix. Anything
// else is "other", which bounds the metric.
func (e *Engine) rule(id string) string {
	for _, seg := range ruleSegments(id) {
		if strings.HasPrefix(seg, HumanGateID) {
			return HumanGateID
		}
		if r := ruleName(seg); e.rules[r] {
			return r
		}
	}
	return "other"
}

// observe counts a completed review's findings and withdrawals by rule,
// logs each critical finding and each withdrawn high or critical one for
// the people who follow them, and announces a human gate once.
func (e *Engine) observe(ctx context.Context, b *Binding, r *Report, a *Assessment, l *slog.Logger) {
	if r == nil || a == nil {
		return
	}
	for _, f := range r.Findings {
		e.m.Findings.WithLabelValues(b.Name, f.Severity, e.rule(f.ID)).Inc()
		if f.Severity == SeverityCritical {
			l.ErrorContext(ctx, "critical finding", "id", f.ID, "path", f.Path, "line", f.Line, "title", oneLine(f.Title, maxTitle))
		}
	}
	for _, w := range a.Withdrawn {
		e.m.Withdrawals.WithLabelValues(b.Name, w.Severity, e.rule(w.ID)).Inc()
		if w.Severity == SeverityCritical || w.Severity == SeverityHigh {
			l.WarnContext(ctx, "severe finding withdrawn", "id", w.ID, "severity", w.Severity,
				"title", oneLine(w.Title, maxTitle), "note", oneLine(w.Note, maxMessage))
		}
	}
	for _, bl := range a.Blocking {
		if bl.ID == HumanGateID {
			e.m.Findings.WithLabelValues(b.Name, SeverityHigh, HumanGateID).Inc()
		}
	}
	if slices.ContainsFunc(a.Blocking, func(x Blocked) bool { return isGate(x.Finding) }) {
		e.notifyGate(ctx, b, l, "the review")
	}
}

// notifyGate announces a head held for a person, once per hold.
func (e *Engine) notifyGate(ctx context.Context, b *Binding, l *slog.Logger, by string) {
	e.m.HumanGates.WithLabelValues(b.Name).Inc()
	l.WarnContext(ctx, "human gate", "held_by", by, "approvers", b.HumanApprovers)
}

// reconcileGate revalidates completed human gates and persists changes.
// It returns the current check (nil on write failure) and whether to defer enqueue.
func (e *Engine) reconcileGate(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (*github.CheckRun, bool) {
	st, ok := LoadState(run.Text)
	if st.Semgrep != nil && (st.Semgrep.NoReport || !st.Semgrep.Verified) {
		return run, true
	}
	if !ok && run.Conclusion != ConclusionSuccess {
		return run, false
	}
	st.Incomplete = st.Incomplete || !ok
	if st.Incomplete && run.Conclusion == ConclusionSuccess {
		// Revoke the unsafe pass before potentially slow history/task reads.
		// Keep current labels even if recovery cannot finish this poll.
		if st.Gate == nil {
			st.Gate = &Gate{}
		}
		st.Gate.observeLabels(gateLabels(b, pr.Labels), e.cfg.Now())
		st.Gate.Approver = ""
		run = e.gateCheck(ctx, b, pr, run, st, ConclusionFailure,
			"Review state incomplete", incompleteStateMessage, nil, e.prLog(b, pr))
		if run == nil {
			return nil, true
		}
	}
	recovered, carried := false, false
	if st.Incomplete {
		before := st
		var rec replay
		st, rec = e.recoverState(ctx, b, pr, run, st)
		recovered = rec == replayDone
		// replayUnread keeps the state incomplete; the next poll retries.
		// A gap keeps it incomplete for good; persist the findings it carries.
		carried = rec == replayGap && (st.Overflow != before.Overflow || len(st.Blocking) != len(before.Blocking) ||
			gateFindings(st.Gate) != gateFindings(before.Gate))
	}
	if recovered && st.IssuesPending {
		return e.holdIssues(ctx, b, pr, run, st, nil), true
	}
	labels := gateLabels(b, pr.Labels)
	gate := &Gate{}
	if st.Gate != nil {
		*gate = *st.Gate
	}
	gate, recovery := e.recoveryGate(b, pr, gate)
	priorNonce := gate.ApprovalNonce
	changed := gate.observeLabels(labels, e.cfg.Now()) || recovery || recovered || carried
	events, err := e.gh.PullRequestLabelEvents(ctx, b.repo(), pr.Number)
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "label_events", err)
		gate, retained := e.retainGate(b, pr, gate, priorNonce != gate.ApprovalNonce)
		changed = changed || retained
		if gate.Needed() {
			st.Gate = gate
		}
		if !changed && run.Conclusion == ConclusionFailure && strings.Contains(run.Summary, labelHistoryMarker) {
			return run, true
		}
		cur := e.gateCheck(ctx, b, pr, run, st, ConclusionFailure, "Waiting for hold history",
			"The reviewer could not read complete label history. It will retry before allowing this head to pass.\n\n"+labelHistoryMarker,
			&CommentUpdate{Verdict: VerdictHold, Headline: "Waiting for complete label history"}, e.prLog(b, pr))
		return cur, true
	}
	changed = gate.observeLabelEvents(b, events) || changed
	gate, retained := e.retainGate(b, pr, gate, priorNonce != gate.ApprovalNonce)
	changed = changed || retained
	if st.Incomplete {
		// No approval or review clears an incomplete state.
		gate.Approver, gate.Authorship = "", ""
		st.Gate = gate
		if changed || run.Conclusion != ConclusionFailure {
			return e.gateCheck(ctx, b, pr, run, st, ConclusionFailure,
				"Unresolved review findings", "The earlier review still blocks this head.",
				&CommentUpdate{Verdict: VerdictChanges, Headline: "Unresolved review findings", Gate: gate}, e.prLog(b, pr)), true
		}
		return run, true
	}
	if !gate.Needed() {
		// Complete label history ends a wait for it; a confirmed context
		// ends a hold an earlier process placed only for its restart.
		history := strings.Contains(run.Summary, labelHistoryMarker)
		if !recovered && !history && (!recovery || run.Conclusion == ConclusionSuccess) &&
			!(len(st.Blocking) > 0 && run.Conclusion != ConclusionFailure) {
			return run, len(st.Blocking) > 0
		}
		title, why := "Hold history checked", "The complete label history contains no human hold."
		if !history {
			title, why = "Review context recovered", "The reviewer confirmed this PR's context from GitHub after its restart, so no human hold remains."
		}
		st.Gate = gate
		if len(st.Blocking) > 0 {
			return e.gateCheck(ctx, b, pr, run, st, ConclusionFailure, "Blocking findings remain",
				why+" The review's other blocking findings still need to be resolved.",
				&CommentUpdate{Verdict: VerdictChanges, Headline: "Blocking findings remain"}, e.prLog(b, pr)), true
		}
		return e.gateCheck(ctx, b, pr, run, st, ConclusionSuccess, title, why,
			&CommentUpdate{Verdict: VerdictApproved, Headline: "No blocking findings"}, e.prLog(b, pr)), true
	}
	// Unreadable approval closes a pass, but creates no new sticky hold.
	before := gate.ApprovalSnapshot
	approver, _ := e.approval(ctx, b, pr, gate)
	changed = changed || before != gate.ApprovalSnapshot
	changed = changed || gate.Approver != approver
	gate.Approver = approver
	changed = changed || (gate.Authorship != "" && !strings.Contains(run.Summary, gate.Authorship))
	changed = changed || (gate.Authorship == "" && strings.Contains(run.Summary, authorshipPrefix))
	conclusion := ConclusionSuccess
	title, summary := "Human gate cleared by "+approver, fmt.Sprintf("%s approved this exact head, which clears its human gate.", approver)
	if gate.Authorship != "" {
		title, summary = "Human gate cleared by authorship", gate.Authorship
	}
	u := &CommentUpdate{Verdict: VerdictApproved, Headline: title, Note: gate.Authorship, Gate: gate}
	if !gate.approved() {
		conclusion = ConclusionFailure
		f := gateFinding(b, gate)
		title, summary = "Waiting for a human review", f.Title+". "+f.Detail+"\n\n"+gateOnlyMarker
		u = &CommentUpdate{Verdict: VerdictHuman, Headline: needsApproval(b), Gate: gate}
		if gate.ApprovalNonce != "" {
			u.Note = acknowledgeNote(gate)
		}
	}
	if len(st.Blocking) > 0 {
		conclusion, title = ConclusionFailure, "Blocking findings remain"
		summary = strings.ReplaceAll(summary, gateOnlyMarker, "") + "\n\nThe review's other blocking findings still need to be resolved."
		u.Verdict, u.Headline = VerdictChanges, title
	}
	if !changed && conclusion == run.Conclusion {
		return run, !gate.approved() || len(st.Blocking) > 0
	}
	l := e.prLog(b, pr)
	st.Gate = gate // Keep the cleared record, session and both evidence bindings.
	cur := e.gateCheck(ctx, b, pr, run, st, conclusion, title, summary, u, l)
	if cur != nil && conclusion == ConclusionFailure && run.Conclusion == ConclusionSuccess {
		e.notifyGate(ctx, b, l, "the current human gate")
	}
	return cur, true
}

// gateCheck posts a new review check run on the head that changes only its
// human gate; the earlier run keeps the review's findings.
func (e *Engine) gateCheck(ctx context.Context, b *Binding, pr github.PullRequest, prev *github.CheckRun, st State, conclusion, title, summary string, u *CommentUpdate, l *slog.Logger) *github.CheckRun {
	now := e.cfg.Now().UTC()
	st.TaskID = e.sourceTask(b, prev, st)
	text := encodeState(st)
	if saved, _ := LoadState(text); saved.Incomplete {
		conclusion, title, summary = ConclusionFailure, "Review state incomplete", incompleteStateMessage
		u = &CommentUpdate{Verdict: VerdictHold, Headline: title, Note: incompleteStateMessage, Gate: st.Gate}
		if st.Gate.held() {
			f := gateFinding(b, st.Gate)
			summary += "\n\n" + f.Title + ". " + f.Detail
		}
	}
	summary += fmt.Sprintf("\n\nThe review's findings are on the earlier `%s` check run", e.cfg.CheckName)
	if prev.URL != "" {
		summary += " (" + prev.URL + ")"
	}
	summary += "."
	summary = keepEnqueueHistory(summary, prev)
	text = recordComment(text, prev.Text, pr.HeadSHA, u)
	run, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
		Status: "completed", Conclusion: conclusion, StartedAt: &now, CompletedAt: &now,
		Output: &github.CheckRunOutput{Title: title, Summary: summary, Text: text},
	})
	if err != nil {
		e.ghError(ctx, l, b, "create_check", err)
		return nil
	}
	e.m.ReviewsCompleted.WithLabelValues(b.Name, conclusion).Inc()
	approver := ""
	if st.Gate != nil {
		approver = st.Gate.Approver
	}
	l.InfoContext(ctx, "human gate check posted", "conclusion", conclusion, "approver", approver)
	e.queueCompletedComment(ctx, b, pr, run, run.Text, u)
	return run
}

// complete finishes the check run. GitHub refusing the annotations (an
// unknown path, say) completes it without them. A failure leaves the check
// in progress; the next poll re-attaches and completes it again.
func (e *Engine) complete(ctx context.Context, b *Binding, run *github.CheckRun, o Outcome, l *slog.Logger) bool {
	now := e.cfg.Now().UTC()
	o.Output.Summary = keepEnqueueHistory(o.Output.Summary, run)
	if o.Conclusion == ConclusionCancelled && o.Output.Text == "" {
		o.Output.Text = run.Text // Cancellation must not erase findings needed after another hold.
	}
	w := github.CheckRunWrite{ExternalID: run.ExternalID, Status: "completed", Conclusion: o.Conclusion, CompletedAt: &now, Output: &o.Output}
	_, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, w)
	var he *github.HTTPError
	if errors.As(err, &he) && he.Status == http.StatusUnprocessableEntity && len(o.Output.Annotations) > 0 {
		l.WarnContext(ctx, "GitHub refused the review's annotations; completing without them", "error", err)
		o.Output.Annotations = nil
		o.Output.Summary += "\n\nGitHub refused this review's line annotations; the findings above are complete."
		_, err = e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, w)
	}
	if err != nil {
		e.ghError(ctx, l, b, "update_check", err)
		l.ErrorContext(ctx, "completing the review check failed; the next poll re-attaches", "conclusion", o.Conclusion)
		if strings.Contains(o.Output.Summary, duplicateHeadMarker) {
			e.rememberHold(headKey{b.Name, run.HeadSHA}, run, o)
		}
		return false
	}
	e.m.ReviewsCompleted.WithLabelValues(b.Name, o.Conclusion).Inc()
	l.InfoContext(ctx, "review check completed", "conclusion", o.Conclusion, "title", o.Output.Title)
	return true
}

// finish completes the check run, then shows the outcome in the PR comment.
func (e *Engine) finish(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, o Outcome, l *slog.Logger) bool {
	o.Output.Text = recordComment(o.Output.Text, run.Text, pr.HeadSHA, o.Comment)
	if !e.complete(ctx, b, run, o, l) {
		return false
	}
	e.queueCompletedComment(ctx, b, pr, run, o.Output.Text, o.Comment)
	return true
}

// stop ends a review whose context ended. Shutdown leaves the check in
// progress for the next process; a superseded or closed head has its task
// cancelled and its check completed as cancelled.
func (e *Engine) stop(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, taskID string, l *slog.Logger) {
	cause := context.Cause(ctx)
	if !errors.Is(cause, errSuperseded) && !errors.Is(cause, errClosed) && !errors.Is(cause, errDuplicateHead) {
		l.Info("review interrupted; its check stays in progress for the next process", "task", taskID)
		return
	}
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	l.InfoContext(cctx, "review cancelled", "task", taskID, "reason", cause.Error())
	e.cancelTask(cctx, b, taskID, l)
	e.complete(cctx, b, run, Outcome{Conclusion: ConclusionCancelled, Output: github.CheckRunOutput{
		Title:   "Review cancelled",
		Summary: "Cancelled: " + cause.Error() + ".\n\n" + e.footer(b, pr, taskID),
	}}, l)
}

func (e *Engine) cancelTask(ctx context.Context, b *Binding, taskID string, l *slog.Logger) {
	if taskID == "" {
		return
	}
	if _, err := e.tasks.CancelTask(ctx, b.Agent, taskID); err != nil {
		e.kagentError(ctx, l, b, "cancel", err)
	}
}

func enqueueKey(b *Binding, pr github.PullRequest) string {
	return fmt.Sprintf("%s#%d@%s", b.repo(), pr.Number, pr.HeadSHA)
}

// reviewPassed requires complete persisted evidence as well as a successful
// conclusion. Callers use only this poll's reconciled check: reconcileGate
// revalidates approval, including authorship evidence that is never persisted.
func reviewPassed(run *github.CheckRun) bool {
	if run == nil || run.Status != "completed" || run.Conclusion != ConclusionSuccess {
		return false
	}
	st, ok := LoadState(run.Text)
	return ok && !st.Incomplete && !st.IssuesPending && len(st.Blocking) == 0 &&
		(st.Semgrep == nil || st.Semgrep.Verified && !st.Semgrep.NoReport)
}

func keepEnqueueHistory(summary string, prev *github.CheckRun) string {
	if prev != nil && strings.Contains(prev.Summary, enqueuedMarker) && !strings.Contains(summary, enqueuedMarker) {
		summary += "\n\n" + enqueuedMarker + "This head was already in the merge queue."
	}
	return summary
}

// maybeEnqueue adds a head whose review passed to the merge queue, once:
// only for an author in EnqueueAuthors, while the required checks are green
// on it, it is still the PR's head, the PR is open, ready and targets the
// base branch, and it is not queued. Other authors' PRs get the check only.
// The check run records the enqueue, so a head the queue sends back is not
// enqueued again; a new push starts over.
func (e *Engine) maybeEnqueue(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (current *github.CheckRun) {
	current = run
	if !reviewPassed(run) || strings.Contains(run.Summary, enqueuedMarker) || !containsFold(b.EnqueueAuthors, pr.Author) {
		return
	}
	key := enqueueKey(b, pr)
	if next, ok := e.enqueueAfter[key]; ok && e.cfg.Now().Before(next) {
		return
	}
	l := e.prLog(b, pr)
	green, err := e.requiredGreen(ctx, b, pr.HeadSHA, l)
	if err != nil || !green {
		return
	}
	q, err := e.gh.PullRequestQueue(ctx, b.repo(), pr.Number)
	if err != nil {
		e.ghError(ctx, l, b, "pull_request", err)
		return nil
	}
	switch {
	case q.State != "open" || q.Draft || q.BaseRef != b.BaseBranch || q.HeadSHA != pr.HeadSHA:
		l.InfoContext(ctx, "not enqueuing: the PR changed since it was listed",
			"state", q.State, "draft", q.Draft, "base", q.BaseRef, "head", q.HeadSHA)
		return nil
	case !slices.Equal(gateLabels(b, pr.Labels), gateLabels(b, q.Labels)):
		pr.Labels = q.Labels
		current, _ = e.reconcileGate(ctx, b, pr, run)
		return // Persist the latest holds before enqueue or group authorization.
	case q.MergeQueueState != "":
		e.m.Enqueues.WithLabelValues(b.Name, "already_queued").Inc()
		l.InfoContext(ctx, "PR already in the merge queue", "queue_state", q.MergeQueueState)
		e.markEnqueued(ctx, b, run, "Already in the merge queue", l)
		return
	}
	state, err := e.gh.EnqueuePullRequest(ctx, b.repo(), q.NodeID, pr.HeadSHA)
	if err != nil {
		e.m.Enqueues.WithLabelValues(b.Name, "error").Inc()
		e.ghError(ctx, l, b, "enqueue", err)
		e.enqueueAfter[key] = e.cfg.Now().Add(e.cfg.EnqueueBackoff)
		return
	}
	delete(e.enqueueAfter, key)
	e.m.Enqueues.WithLabelValues(b.Name, "enqueued").Inc()
	l.InfoContext(ctx, "PR enqueued", "queue_state", state)
	e.markEnqueued(ctx, b, run, "Entered the merge queue", l)
	return
}

func (e *Engine) markEnqueued(ctx context.Context, b *Binding, run *github.CheckRun, what string, l *slog.Logger) {
	limit := maxSummary
	if strings.Contains(run.Summary, commentLimitNote) {
		limit += len(commentLimitNote) + 2
	}
	summary := clip(run.Summary, limit) + fmt.Sprintf("\n\n%s%s at %s.", enqueuedMarker, what, e.cfg.Now().UTC().Format(time.RFC3339))
	_, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, github.CheckRunWrite{
		Output: &github.CheckRunOutput{Title: run.Title, Summary: summary, Text: run.Text},
	})
	if err != nil {
		e.ghError(ctx, l, b, "update_check", err)
	}
}

// requiredGreen reports whether every required check succeeded on sha.
func (e *Engine) requiredGreen(ctx context.Context, b *Binding, sha string, l *slog.Logger) (bool, error) {
	var statuses []github.Status
	fetched := false
	for _, c := range b.RequiredChecks {
		if c.AppID != 0 {
			runs, err := e.gh.ListCheckRuns(ctx, b.repo(), sha, github.CheckRunFilter{Name: c.Name, AppID: c.AppID})
			if err != nil {
				e.ghError(ctx, l, b, "check_runs", err)
				return false, err
			}
			ok := false
			for _, r := range runs {
				ok = ok || (r.Name == c.Name && r.AppID == c.AppID && r.HeadSHA == sha && r.Status == "completed" && r.Conclusion == ConclusionSuccess)
			}
			if !ok {
				return false, nil
			}
			continue
		}
		if !fetched {
			var err error
			if statuses, err = e.gh.Statuses(ctx, b.repo(), sha); err != nil {
				e.ghError(ctx, l, b, "statuses", err)
				return false, err
			}
			fetched = true
		}
		ok := false
		for _, s := range statuses {
			ok = ok || (s.Context == c.Name && strings.EqualFold(s.Creator, c.StatusCreator) && s.SHA == sha && s.State == "success")
		}
		if !ok {
			return false, nil
		}
	}
	return true, nil
}

// maxGroups bounds the merge group commits remembered.
const maxGroups = 1000

// mergeGroups posts the review check on each merge group commit, whose
// required checks GitHub evaluates again: success when the queued PR's head
// passed review, failure otherwise. A group posted as passing is checked on
// every poll: once its PR's head stops passing (a hold label or a newer
// review), the group gets a failing check, so the queue cannot merge it on
// the earlier pass. The group's other PRs are ahead of it in the queue and
// carry their own groups.
func (e *Engine) mergeGroups(ctx context.Context, b *Binding, reviews map[int]*github.CheckRun) {
	repo := b.repo()
	entries, err := e.gh.MergeQueueEntries(ctx, repo, b.BaseBranch)
	if err != nil {
		e.ghError(ctx, e.log.With("binding", b.Name, "repo", repo), b, "merge_queue", err)
		return
	}
	if len(e.groups) > maxGroups {
		clear(e.groups)
	}
	for _, en := range entries {
		key := repo + "@" + en.HeadCommit
		if en.HeadCommit == "" || e.groups[key] == ConclusionFailure {
			continue // a failed group cannot merge; GitHub removes it
		}
		l := e.log.With("binding", b.Name, "repo", repo, "pr", en.Number, "sha", en.PRHeadSHA, "merge_group", en.HeadCommit)
		posted := e.groups[key]
		if posted == "" {
			existing, err := e.reviewCheck(ctx, b, en.HeadCommit)
			if err != nil {
				continue
			}
			if existing != nil {
				posted = ConclusionFailure
				if existing.Status == "completed" && existing.Conclusion == ConclusionSuccess {
					posted = ConclusionSuccess
				}
				e.groups[key] = posted
				if posted == ConclusionFailure {
					continue
				}
			}
		}
		review := reviews[en.Number] // Only this poll can authorize the queued head.
		passed := reviewPassed(review) && review.HeadSHA == en.PRHeadSHA
		o := github.CheckRunOutput{
			Title:   "Reviewed in the queued PR",
			Summary: fmt.Sprintf("This merge group commit adds PR #%d, whose head %s passed `%s`.", en.Number, short(en.PRHeadSHA), e.cfg.CheckName),
		}
		conclusion := ConclusionSuccess
		switch {
		case posted == "" && !passed:
			conclusion = ConclusionFailure
			o.Title = "Queued PR not reviewed"
			o.Summary = fmt.Sprintf("PR #%d's head %s has no passing `%s`, so this merge group cannot merge.", en.Number, short(en.PRHeadSHA), e.cfg.CheckName)
		case posted == "":
		case !passed:
			// The queued head stopped passing after its group's check, say
			// for a hold label: the group must not merge on the earlier pass.
			conclusion = ConclusionFailure
			o.Title = "Queued PR no longer passes"
			o.Summary = fmt.Sprintf("PR #%d's head %s no longer passes `%s` (a hold or a newer review), so this merge group cannot merge.",
				en.Number, short(en.PRHeadSHA), e.cfg.CheckName)
		default:
			continue // posted and still passing
		}
		now := e.cfg.Now().UTC()
		_, err = e.gh.CreateCheckRun(ctx, repo, github.CheckRunWrite{
			Name: e.cfg.CheckName, HeadSHA: en.HeadCommit, Status: "completed", Conclusion: conclusion,
			StartedAt: &now, CompletedAt: &now, Output: &o,
		})
		if err != nil {
			e.ghError(ctx, l, b, "create_check", err)
			continue
		}
		e.groups[key] = conclusion
		e.m.MergeGroupChecks.WithLabelValues(b.Name, conclusion).Inc()
		l.InfoContext(ctx, "merge group check posted", "conclusion", conclusion)
	}
}

func (e *Engine) ghError(ctx context.Context, l *slog.Logger, b *Binding, call string, err error) {
	if ctx.Err() != nil {
		return // shutting down
	}
	e.m.GitHubErrors.WithLabelValues(b.Name, call).Inc()
	l.WarnContext(ctx, "GitHub call failed", "call", call, "error", err)
}

func (e *Engine) kagentError(ctx context.Context, l *slog.Logger, b *Binding, call string, err error) {
	if ctx.Err() != nil {
		return
	}
	e.m.KagentErrors.WithLabelValues(b.Name, call).Inc()
	l.WarnContext(ctx, "kagent call failed", "call", call, "error", err)
}

func short(sha string) string { return sha[:min(12, len(sha))] }
