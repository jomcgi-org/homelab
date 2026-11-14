// Package review is the factory reviewer's loop. It polls each binding's
// open PRs into the base branch, keeps a `factory review` check run on every
// head, sends each new head to the binding's kagent reviewer agent,
// completes the check from the agent's findings and enqueues heads that pass
// with required CI green. Check runs are its only state
// (projects/factory/ARCHITECTURE.md, "Merge authority"). It holds the
// reviewer App's token; the agent never sees it.
package review

import (
	"context"
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

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
)

// GitHub is the part of the GitHub client the reviewer uses.
type GitHub interface {
	ListPullRequests(ctx context.Context, repo, base string) ([]github.PullRequest, bool, error)
	ListCheckRuns(ctx context.Context, repo, sha string, f github.CheckRunFilter) ([]github.CheckRun, error)
	CreateCheckRun(ctx context.Context, repo string, w github.CheckRunWrite) (*github.CheckRun, error)
	UpdateCheckRun(ctx context.Context, repo string, id int64, w github.CheckRunWrite) (*github.CheckRun, error)
	PullRequestFiles(ctx context.Context, repo string, number int) ([]github.PullRequestFile, error)
	PullRequestHeads(ctx context.Context, repo string, number int, f github.CheckRunFilter) (*github.HeadHistory, error)
	PullRequestReviews(ctx context.Context, repo string, number int) ([]github.PullRequestReview, error)
	PullRequestComments(ctx context.Context, repo string, number int) ([]github.Comment, error)
	CompareCommits(ctx context.Context, repo, base, head string) (*github.CommitComparison, error)
	Statuses(ctx context.Context, repo, sha string) ([]github.Status, error)
	PullRequestQueue(ctx context.Context, repo string, number int) (*github.PullRequestQueue, error)
	EnqueuePullRequest(ctx context.Context, repo, nodeID, headSHA string) (string, error)
	MergeQueueEntries(ctx context.Context, repo, branch string) ([]github.MergeQueueEntry, error)
	RulesReader
}

// Tasks is the part of the kagent A2A client the reviewer uses.
type Tasks interface {
	Send(ctx context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error)
	GetTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
	CancelTask(ctx context.Context, agent kagent.AgentRef, taskID string) (*kagent.Task, error)
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
	Name              string
	Owner             string
	Repo              string
	BaseBranch        string
	Agent             kagent.AgentRef
	RequiredChecks    []Check
	SkipLabels        []string
	EnqueueAuthors    []string
	HumanGatePaths    []string
	HumanGateLabels   []string
	HumanApprovers    []string
	TrustedCommenters []string
	ContextDocs       []string
	GeneratedPaths    []string
	MaxDiffLines      int
	MaxDiffFiles      int
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
}

// Engine runs the reviewer.
type Engine struct {
	cfg   Config
	gh    GitHub
	tasks Tasks
	m     *metrics.Metrics
	log   *slog.Logger
	rules map[string]bool

	mu       sync.Mutex
	inflight map[headKey]*running
	wg       sync.WaitGroup

	// Only the polling goroutine uses these.
	groups       map[string]bool      // merge group commits that carry a review check
	enqueueAfter map[string]time.Time // repo#pr@sha: next enqueue attempt after a failure
}

// headKey names a review: check runs belong to a commit, so two open PRs
// with the same head share one.
type headKey struct {
	binding string
	sha     string
}

type running struct {
	number int
	cancel context.CancelCauseFunc
	// unlisted counts the complete listings in a row that left out both
	// the head and the PR.
	unlisted int
}

// closedAfter is how many complete listings in a row must leave out a PR
// before its review is cancelled as closed: a PR that closes between two
// page reads shifts the next one off both pages.
const closedAfter = 2

var (
	errSuperseded = errors.New("a newer head of the PR superseded this one")
	errClosed     = errors.New("the PR closed or no longer targets the base branch")
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
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	for i := range cfg.Bindings {
		b := &cfg.Bindings[i]
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
		cfg: cfg, gh: gh, tasks: tasks, m: m, log: log, rules: rules,
		inflight: map[headKey]*running{}, groups: map[string]bool{}, enqueueAfter: map[string]time.Time{},
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

// Wait returns once every running review has returned.
func (e *Engine) Wait() { e.wg.Wait() }

// Tick polls every binding once. Reviews it starts run in the background
// under ctx.
func (e *Engine) Tick(ctx context.Context) {
	for i := range e.cfg.Bindings {
		if ctx.Err() != nil {
			return
		}
		e.tickBinding(ctx, &e.cfg.Bindings[i])
	}
}

type candidate struct {
	pr  github.PullRequest
	run *github.CheckRun
}

func (e *Engine) tickBinding(ctx context.Context, b *Binding) {
	repo := b.repo()
	bl := e.log.With("binding", b.Name, "repo", repo)
	prs, complete, err := e.gh.ListPullRequests(ctx, repo, b.BaseBranch)
	if err != nil {
		e.ghError(ctx, bl, b, "list_pulls", err)
		return
	}
	if !complete {
		bl.WarnContext(ctx, "the open PR listing is longer than the client reads; PRs past it are not reviewed this poll", "listed", len(prs))
	}
	e.supersede(b, prs, complete)
	prioritize(b, prs)

	var attach, start []candidate
	reviews := map[int]*github.CheckRun{}
	for _, pr := range prs {
		if pr.BaseRef != b.BaseBranch || pr.HeadSHA == "" || pr.Draft || b.skipped(&pr) || e.isRunning(headKey{b.Name, pr.HeadSHA}) {
			continue
		}
		run, err := e.reviewCheck(ctx, b, pr.HeadSHA)
		if err != nil {
			continue
		}
		reviews[pr.Number] = run
		switch {
		case run == nil || (run.Status == "completed" && run.Conclusion == ConclusionCancelled):
			// A cancelled review is no review: the head is current again.
			start = append(start, candidate{pr, nil})
		case run.Status != "completed":
			attach = append(attach, candidate{pr, run})
		case e.reconcileGate(ctx, b, pr, run):
			// Held for a person, or the check just changed.
		case run.Conclusion == ConclusionSuccess:
			e.maybeEnqueue(ctx, b, pr, run)
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

// begin starts a review in the background, creating its check run first
// when the head has none. It reports true, starting nothing, when every
// slot is taken; a head already under review starts nothing either.
func (e *Engine) begin(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) (full bool) {
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
	e.inflight[key] = &running{number: pr.Number, cancel: cancel}
	e.m.InFlight.Set(float64(len(e.inflight)))
	e.mu.Unlock()

	l := e.prLog(b, pr)
	kind := "reattach"
	if run == nil {
		kind = "new"
		now := e.cfg.Now().UTC()
		created, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
			Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
			Status: "in_progress", StartedAt: &now,
			Output: &github.CheckRunOutput{Title: "Review in progress", Summary: progressSummary(b, nil, recorded{}, "")},
		})
		if err != nil {
			e.ghError(ctx, l, b, "create_check", err)
			cancel(nil)
			e.done(key)
			return false
		}
		if created.StartedAt.IsZero() {
			created.StartedAt = now
		}
		run = created
		l.InfoContext(ctx, "review check created", "check_run", run.ID)
	} else {
		l.InfoContext(ctx, "re-attaching to an in-progress review", "check_run", run.ID)
	}
	e.m.ReviewsStarted.WithLabelValues(b.Name, kind).Inc()
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		defer e.done(key)
		defer cancel(nil)
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
}

// The task marker is written only by the service; markerStart is stripped
// from every text the agent or the PR supplies, and the last marker counts.
var taskMarker = regexp.MustCompile(`<!-- factory-review:task attempt=([0-9]+) id=([^\s>]*) sent=([0-9]*) -->`)

// RecordedTask is the attempt and kagent task an in-progress review check
// run records.
func RecordedTask(summary string) recorded {
	all := taskMarker.FindAllStringSubmatch(summary, -1)
	if len(all) == 0 {
		return recorded{}
	}
	m := all[len(all)-1]
	n, _ := strconv.Atoi(m[1])
	rec := recorded{Attempt: max(n, 1), ID: m[2]}
	if sec, err := strconv.ParseInt(m[3], 10, 64); err == nil && sec > 0 {
		rec.Sent = time.Unix(sec, 0)
	}
	return rec
}

// progressSummary is an in-progress check's summary: what is running, why
// an earlier attempt ended, the head's human gate, and the attempt marker.
func progressSummary(b *Binding, gate *Gate, rec recorded, note string) string {
	s := fmt.Sprintf("The kagent agent `%s/%s` is reviewing this commit.", b.Agent.Namespace, b.Agent.Name)
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
		s += fmt.Sprintf(".\n\n<!-- factory-review:task attempt=%d id=%s sent=%d -->", rec.Attempt, rec.ID, sent)
	}
	return s
}

// inputs are what a review is judged with, read before its task is sent.
type inputs struct {
	files     []github.PullRequestFile
	size      DiffSize
	gate      *Gate
	prior     State
	priorHead string
	// priorUnknown says why an earlier head's review could not be found.
	priorUnknown string
}

// review drives one head's review to a completed check run. It makes at
// most MaxAttempts attempts. Each builds its message once and sends those
// exact bytes until kagent answers with a task, which runs in a fresh
// session for at most Timeout. An attempt that ends without a report (a
// failed, rejected or timed-out task, an unusable reply, or reads that kept
// failing) is followed by the next one after RetryBackoff; the last one
// fails the check. A restart finds the task the check run records, else
// sends the recorded attempt's message again.
func (e *Engine) review(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, l *slog.Logger) {
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
	)
	// next ends the attempt without a report. It reports true when that was
	// the last attempt and the check is complete.
	next := func(reason string) bool {
		l.WarnContext(ctx, "review attempt ended without a report", "attempt", attempt, "task", rec.ID, "reason", reason)
		if attempt >= MaxAttempts {
			e.complete(ctx, b, run, NoReport(fmt.Sprintf("Attempt %d of %d ended without a report: %s", attempt, MaxAttempts, reason),
				e.footer(b, pr, rec.ID)), l)
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
			case late:
				if next("the reviewer could not read the PR from GitHub.") {
					return
				}
				continue
			default:
				e.sleep(ctx, e.cfg.Reissue)
				continue
			}
			if rec.ID == "" && in.size.over(b) {
				l.WarnContext(ctx, "the PR is over the review budget; asking for a split",
					"lines", in.size.Lines, "files", in.size.Files, "generated", in.size.Generated)
				e.m.SplitRequests.WithLabelValues(b.Name).Inc()
				e.complete(ctx, b, run, SplitOutcome(b, in.size, e.footer(b, pr, "")), l)
				return
			}
		}
		if rec.ID == "" {
			if msg == nil {
				m, err := e.message(ctx, b, pr, run, in, attempt, l)
				switch {
				case err == nil:
					msg, sends = m, 0
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
			t, err := e.tasks.Send(ctx, b.Agent, *msg)
			sends++
			lastSend = e.cfg.Now()
			switch {
			case err == nil:
				rec = recorded{Attempt: attempt, ID: t.ID, Sent: lastSend}
				l.InfoContext(ctx, "review task sent", "message_id", msg.MessageID, "task", t.ID, "attempt", attempt, "sends", sends)
				e.recordProgress(ctx, b, run, in.gate, rec, "", l)
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
		case err == nil && kagent.Quiescent(t.Status.State):
			res := e.outcome(ctx, b, pr, t, in, l)
			if res.reason != "" {
				if next(res.reason) {
					return
				}
				continue
			}
			if e.complete(ctx, b, run, res.o, l) {
				e.observe(ctx, b, res.r, res.a, l)
			}
			return
		case errors.Is(err, kagent.ErrTaskNotFound):
			// Expired or never seen: the attempt's message finds or opens it.
			l.WarnContext(ctx, "review task not found; sending its message again", "task", rec.ID)
			e.m.KagentErrors.WithLabelValues(b.Name, "get_task").Inc()
			rec = recorded{Attempt: attempt}
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
		l.WarnContext(ctx, "the previous review could not be found; asking for a full review", "reason", unknown)
	}
	return &inputs{
		files: files, size: measure(b, files), gate: humanGate(b, changedPaths(files), pr.Labels),
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
	seen := map[string]bool{pr.HeadSHA: true}
	looked := 0
	for i := len(hist.Heads) - 1; i >= 0; i-- {
		h := hist.Heads[i]
		if seen[h.SHA] || !h.Checked {
			continue
		}
		seen[h.SHA] = true
		if looked == maxPreviousLookups {
			return State{}, "", fmt.Sprintf("more than %d earlier heads carry a review check", maxPreviousLookups), nil
		}
		looked++
		run, err := e.reviewCheck(ctx, b, h.SHA)
		if err != nil {
			return State{}, "", "", err
		}
		if run == nil || run.Status != "completed" {
			continue
		}
		if st, ok := LoadState(run.Text); ok {
			return st, h.SHA, "", nil
		}
	}
	if hist.Truncated {
		return State{}, "", "its timeline has more commit and push events than the reviewer reads", nil
	}
	return State{}, "", "", nil
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
		case strings.TrimSpace(c.Body) == "":
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
// kagent rejects a message ID sent again with other content. The message
// names no session, so kagent opens a fresh one for it.
func (e *Engine) message(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, in *inputs, attempt int, l *slog.Logger) (*kagent.Message, error) {
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
	var interdiff *github.CommitComparison
	if in.priorHead != "" {
		interdiff, err = e.gh.CompareCommits(ctx, repo, in.priorHead, pr.HeadSHA)
		if err != nil && !errors.Is(err, github.ErrNotFound) {
			e.ghError(ctx, l, b, "compare", err)
			return nil, err
		}
	}
	text := Prompt(PromptInput{
		Repo: repo, Number: pr.Number, URL: pr.URL, Base: b.BaseBranch, SHA: pr.HeadSHA,
		Title: pr.Title, Body: pr.Body, Author: pr.Author, Files: in.files, Rules: rules,
		Previous: in.prior.Previous(), PreviousHead: in.priorHead, Interdiff: interdiff, PreviousUnknown: in.priorUnknown,
		Comments: comments, DroppedComments: dropped, Gate: in.gate, Attempt: attempt,
	})
	msg := &kagent.Message{
		MessageID: AttemptMessageID(repo, pr.Number, pr.HeadSHA, run.ID, attempt),
		Parts:     []kagent.Part{{Text: text}},
		Metadata: map[string]any{
			"factory.jomcgi.dev/repository":   repo,
			"factory.jomcgi.dev/pull-request": pr.Number,
			"factory.jomcgi.dev/head":         pr.HeadSHA,
		},
	}
	l.InfoContext(ctx, "review task built", "message_id", msg.MessageID, "attempt", attempt,
		"rules", rules.Paths(), "rules_commit", rules.Commit, "previous_head", in.priorHead, "previous_unknown", in.priorUnknown,
		"previous_findings", len(in.prior.Previous()), "trusted_comments", len(comments), "dropped_comments", dropped)
	return msg, nil
}

// recordProgress writes the attempt, and its task once sent, on the
// in-progress check run, so a restart reads the task instead of sending a
// message whose PR text may have changed.
func (e *Engine) recordProgress(ctx context.Context, b *Binding, run *github.CheckRun, gate *Gate, rec recorded, note string, l *slog.Logger) {
	_, err := e.gh.UpdateCheckRun(ctx, b.repo(), run.ID, github.CheckRunWrite{
		Output: &github.CheckRunOutput{Title: "Review in progress", Summary: progressSummary(b, gate, rec, note)},
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
	o      Outcome
	r      *Report
	a      *Assessment
	reason string
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
		a := Assess(r, in.prior.Previous())
		gate := &Gate{}
		if in.gate != nil {
			*gate = *in.gate
		}
		for _, f := range a.Gated {
			if len(gate.Findings) < maxGatePaths {
				gate.Findings = append(gate.Findings, storable(f))
			}
		}
		if !gate.Needed() {
			gate = nil
		} else {
			if gate.Approver, err = e.approval(ctx, b, pr); err != nil {
				l.WarnContext(ctx, "could not read the PR's reviews; the human gate stays closed", "error", err)
			}
			if gate.Approver != "" {
				a.Approved, a.Approver = gate.Findings, gate.Approver
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
		a.Conclusion = ConclusionSuccess
		if len(a.Blocking) > 0 {
			a.Conclusion = ConclusionFailure
		}
		o := Render(a, r, gate, footer)
		if in.priorUnknown != "" {
			o.Output.Summary = clip(fmt.Sprintf("The review of an earlier head could not be found (%s), so this head was reviewed in full.\n\n",
				in.priorUnknown)+o.Output.Summary, maxSummary)
		}
		return result{o: o, r: r, a: &a}
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
// clears its gate, or empty.
func (e *Engine) approval(ctx context.Context, b *Binding, pr github.PullRequest) (string, error) {
	reviews, err := e.gh.PullRequestReviews(ctx, b.repo(), pr.Number)
	if err != nil {
		e.ghError(ctx, e.prLog(b, pr), b, "reviews", err)
		return "", err
	}
	return approvedBy(b, reviews, pr.HeadSHA), nil
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

// reconcileGate keeps a completed check in line with its head's human gate
// once the model's findings no longer block: an APPROVED review from a
// configured human on the head passes it, and a gate label added after a
// pass fails it. Each change is a new check run on the head. It reports
// true while the head is held for a person or its check just changed.
func (e *Engine) reconcileGate(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) bool {
	st, ok := LoadState(run.Text)
	if !ok || len(st.Blocking) > 0 || (st.Gate != nil && st.Gate.Approver != "") {
		return false
	}
	labels := gateLabels(b, pr.Labels)
	if st.Gate == nil && len(labels) == 0 {
		return false
	}
	gate := &Gate{}
	if st.Gate != nil {
		*gate = *st.Gate
	}
	for _, lb := range labels {
		if !slices.Contains(gate.Labels, lb) {
			gate.Labels = append(gate.Labels, lb)
		}
	}
	approver, err := e.approval(ctx, b, pr)
	if err != nil {
		return true
	}
	l := e.prLog(b, pr)
	gate.Approver = approver
	switch {
	case approver != "" && run.Conclusion != ConclusionSuccess:
		e.gateCheck(ctx, b, pr, run, State{Blocking: st.Blocking, Gate: gate}, ConclusionSuccess,
			"Human gate cleared by "+approver,
			fmt.Sprintf("%s approved this exact head, which clears its human gate.", approver), l)
		return true
	case approver == "" && run.Conclusion == ConclusionSuccess:
		f := gateFinding(b, gate)
		e.gateCheck(ctx, b, pr, run, State{Blocking: st.Blocking, Gate: gate}, ConclusionFailure,
			"Waiting for a human review", f.Title+". "+f.Detail+"\n\n"+gateOnlyMarker, l)
		e.notifyGate(ctx, b, l, "a gate label")
	}
	return approver == ""
}

// gateCheck posts a new review check run on the head that changes only its
// human gate; the earlier run keeps the review's findings.
func (e *Engine) gateCheck(ctx context.Context, b *Binding, pr github.PullRequest, prev *github.CheckRun, st State, conclusion, title, summary string, l *slog.Logger) {
	now := e.cfg.Now().UTC()
	summary += fmt.Sprintf("\n\nThe review's findings are on the earlier `%s` check run", e.cfg.CheckName)
	if prev.URL != "" {
		summary += " (" + prev.URL + ")"
	}
	_, err := e.gh.CreateCheckRun(ctx, b.repo(), github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
		Status: "completed", Conclusion: conclusion, StartedAt: &now, CompletedAt: &now,
		Output: &github.CheckRunOutput{Title: title, Summary: summary + ".", Text: encodeState(st)},
	})
	if err != nil {
		e.ghError(ctx, l, b, "create_check", err)
		return
	}
	e.m.ReviewsCompleted.WithLabelValues(b.Name, conclusion).Inc()
	l.InfoContext(ctx, "human gate check posted", "conclusion", conclusion, "approver", st.Gate.Approver)
}

// complete finishes the check run. GitHub refusing the annotations (an
// unknown path, say) completes it without them. A failure leaves the check
// in progress; the next poll re-attaches and completes it again.
func (e *Engine) complete(ctx context.Context, b *Binding, run *github.CheckRun, o Outcome, l *slog.Logger) bool {
	now := e.cfg.Now().UTC()
	w := github.CheckRunWrite{Status: "completed", Conclusion: o.Conclusion, CompletedAt: &now, Output: &o.Output}
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
		return false
	}
	e.m.ReviewsCompleted.WithLabelValues(b.Name, o.Conclusion).Inc()
	l.InfoContext(ctx, "review check completed", "conclusion", o.Conclusion, "title", o.Output.Title)
	return true
}

// stop ends a review whose context ended. Shutdown leaves the check in
// progress for the next process; a superseded or closed head has its task
// cancelled and its check completed as cancelled.
func (e *Engine) stop(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun, taskID string, l *slog.Logger) {
	cause := context.Cause(ctx)
	if !errors.Is(cause, errSuperseded) && !errors.Is(cause, errClosed) {
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

// maybeEnqueue adds a head whose review passed to the merge queue, once:
// only for an author in EnqueueAuthors, while the required checks are green
// on it, it is still the PR's head, the PR is open, ready and targets the
// base branch, and it is not queued. Other authors' PRs get the check only.
// The check run records the enqueue, so a head the queue sends back is not
// enqueued again; a new push starts over.
func (e *Engine) maybeEnqueue(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) {
	if strings.Contains(run.Summary, enqueuedMarker) || !containsFold(b.EnqueueAuthors, pr.Author) {
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
		return
	}
	switch {
	case q.State != "open" || q.Draft || q.BaseRef != b.BaseBranch || q.HeadSHA != pr.HeadSHA:
		l.InfoContext(ctx, "not enqueuing: the PR changed since it was listed",
			"state", q.State, "draft", q.Draft, "base", q.BaseRef, "head", q.HeadSHA)
		return
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
}

func (e *Engine) markEnqueued(ctx context.Context, b *Binding, run *github.CheckRun, what string, l *slog.Logger) {
	summary := clip(run.Summary, maxSummary) + fmt.Sprintf("\n\n%s%s at %s.", enqueuedMarker, what, e.cfg.Now().UTC().Format(time.RFC3339))
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
// passed review, failure otherwise. The group's other PRs are ahead of it
// in the queue and carry their own groups.
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
		if en.HeadCommit == "" || e.groups[key] {
			continue
		}
		l := e.log.With("binding", b.Name, "repo", repo, "pr", en.Number, "sha", en.PRHeadSHA, "merge_group", en.HeadCommit)
		existing, err := e.reviewCheck(ctx, b, en.HeadCommit)
		if err != nil {
			continue
		}
		if existing != nil {
			e.groups[key] = true
			continue
		}
		review, ok := reviews[en.Number]
		if !ok || (review != nil && review.HeadSHA != en.PRHeadSHA) {
			if review, err = e.reviewCheck(ctx, b, en.PRHeadSHA); err != nil {
				continue
			}
		}
		passed := review != nil && review.HeadSHA == en.PRHeadSHA && review.Status == "completed" && review.Conclusion == ConclusionSuccess
		o := github.CheckRunOutput{
			Title:   "Reviewed in the queued PR",
			Summary: fmt.Sprintf("This merge group commit adds PR #%d, whose head %s passed `%s`.", en.Number, short(en.PRHeadSHA), e.cfg.CheckName),
		}
		conclusion := ConclusionSuccess
		if !passed {
			conclusion = ConclusionFailure
			o.Title = "Queued PR not reviewed"
			o.Summary = fmt.Sprintf("PR #%d's head %s has no passing `%s`, so this merge group cannot merge.", en.Number, short(en.PRHeadSHA), e.cfg.CheckName)
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
		e.groups[key] = true
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
