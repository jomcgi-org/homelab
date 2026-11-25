package review

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// A head whose git tree equals the tree of the PR's last reviewed head holds
// the same files, so its code review would read the same diff. The service
// then keeps the earlier head's findings instead of starting an agent turn
// (Config.ReuseUnchangedTree). Everything else a head's verdict reads is
// read again: the goal check, the human gate and approval, Joe's decision
// comments (revalidated), labels, the Semgrep evidence and the issue
// evidence. Reuse fails closed: any input the code review saw that could
// differ, or cannot be proved equal, gives the head a full review.

// PathReused is the sessions metric's path for a head whose earlier code
// review was kept; no kagent task runs for it.
const PathReused = "reused"

// reasonUnchangedTree is its reason label.
const reasonUnchangedTree = "unchanged_tree"

// Why a head is not reused. They are logged, not metric labels.
const (
	notReusedNoPrior   = "no earlier reviewed head"
	notReusedState     = "the earlier head's review state is incomplete or unreadable"
	notReusedBasis     = "the earlier review did not record its inputs"
	notReusedRead      = "the reviewer could not read what the code review depends on"
	notReusedInputs    = "the title, description, trusted comments, rules, reviewer or prompts changed since the earlier review, or the agent prompt revision is unreadable"
	notReusedTree      = "the tree differs from the earlier reviewed head's"
	notReusedMergeBase = "the merge base differs from the earlier reviewed head's"
	notReusedCommits   = "a commit since the earlier review answers a finding or carries an activation"
	notReusedOwned     = "an earlier blocking finding asks for evidence or a hold the agent must account for"
)

// reuse is the earlier head's code review a head keeps.
type reuse struct {
	head string
	task *kagent.Task
}

// reviewPromptVersion names how this reviewer binary builds the review task.
// Raise it with a change to the task or to what the agent is told that
// promptFingerprint cannot see (a branch its fixed inputs do not reach). A new
// value gives every PR's next head a full review.
var reviewPromptVersion = "1"

// promptFingerprint digests the task text Prompt builds for fixed inputs
// (fingerprintInputs), so an edit to the binary's own prompt text (Prompt,
// laterRule) changes the basis without a version change.
var promptFingerprint = sync.OnceValue(func() string {
	h := sha256.New()
	for _, in := range fingerprintInputs() {
		h.Write([]byte(Prompt(in)))
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil)[:16])
})

// fingerprintInputs are a first review, a later head with a scoped interdiff,
// and that later head resumed in the earlier session.
func fingerprintInputs() []PromptInput {
	first := PromptInput{Repo: "o/r", Number: 1, Base: "main", SHA: "a", Title: "t", Body: "b", Rules: &Rules{Commit: "fingerprint"}, Attempt: 1, CheckName: "c"}
	later := first
	later.SHA, later.PreviousHead = "b", "a"
	later.Previous = []Finding{{ID: "x.go:correctness", Severity: SeverityHigh, Path: "x.go", Title: "t"}}
	later.Interdiff, later.Scope = &github.CommitComparison{Status: "ahead"}, &Scope{}
	resumed := later
	resumed.Resumed, resumed.RulesHeld = true, true
	return []PromptInput{first, later, resumed}
}

// promptRevision is the kagent Agent revision that serves the binding's system
// prompt, or "" when it cannot be read. An empty revision gives an empty
// basis.
func (e *Engine) promptRevision(ctx context.Context, b *Binding, l *slog.Logger) string {
	if e.cfg.Sessions == nil || b.Runtime == nil {
		return ""
	}
	rev, err := e.cfg.Sessions.PromptRevision(ctx, b.Agent, b.Runtime.SystemPrompt)
	if err != nil {
		e.kagentError(ctx, l, b, "prompt_revision", err)
		return ""
	}
	return rev
}

// reviewBasis digests what the code review reads besides the tree: the
// binding and its runtime (model, effort, arm, system prompt), the kagent
// Agent revision that serves that prompt, this binary's prompt version and
// fingerprint, the PR's title and description, the trusted comments and the
// rules. It is empty when the rules digest or the revision is, which keeps
// the review from being reused. The State stores it with each review, so an
// edited description, a new or edited comment (Joe's decisions among them:
// the agent must read one before it clears a finding, REVIEW.md "Clearing a
// finding without a fix"), a rules change, a new agent prompt or a new
// reviewer prompt gives the next head a full review.
func reviewBasis(b *Binding, pr github.PullRequest, comments []github.Comment, rules *Rules, revision string) string {
	digest := rulesDigest(rules)
	if digest == "" || revision == "" || b.Runtime == nil {
		return ""
	}
	type comment struct {
		ID           int64
		Author, Body string
	}
	// SystemPrompt is named here: provenance.Runtime leaves it out of JSON.
	in := struct {
		Binding, Agent, Title, Body, Rules string
		Runtime                            any
		SystemPrompt, Revision, Prompt     string
		Comments                           []comment
	}{
		Binding: b.Name, Agent: agentName(b.Agent), Title: pr.Title, Body: pr.Body, Rules: digest, Runtime: b.Runtime,
		SystemPrompt: b.Runtime.SystemPrompt, Revision: revision, Prompt: reviewPromptVersion + "/" + promptFingerprint(),
	}
	for _, c := range comments {
		in.Comments = append(in.Comments, comment{c.ID, c.Author, c.Body})
	}
	h := sha256.New()
	if err := json.NewEncoder(h).Encode(in); err != nil {
		return ""
	}
	return hex.EncodeToString(h.Sum(nil)[:16])
}

// reusable decides whether pr's head keeps the code review of the earlier
// head in.priorHead. It returns "" and sets in.reuse when it does, else the
// reason it does not.
func (e *Engine) reusable(ctx context.Context, b *Binding, pr github.PullRequest, in *inputs, l *slog.Logger) string {
	p := in.prior
	switch {
	case in.priorHead == "" || in.priorHead == pr.HeadSHA || p.TaskID == "":
		return notReusedNoPrior
	case in.priorUnknown != "" || p.Incomplete || p.Overflow || p.IssuesPending || (p.Semgrep != nil && p.Semgrep.NoReport):
		return notReusedState
	case p.Basis == "":
		return notReusedBasis
	}
	for _, f := range p.Blocking {
		// The service re-derives goal findings (applyGoal). Any other finding
		// that is no plain defect is cleared by evidence or by someone acting,
		// which only an agent's account of it can record.
		if !goalFinding(b, f) && (findingKind(f) != KindDefect || f.ID == "activation:goal") {
			return notReusedOwned
		}
	}
	repo := b.repo()
	rules, err := LoadRules(ctx, e.gh, repo, b.BaseBranch, changedPaths(in.files), b.ContextDocs)
	if err != nil {
		e.ghError(ctx, l, b, "rules", err)
		return notReusedRead
	}
	comments, _, err := e.trustedComments(ctx, b, pr)
	if err != nil {
		e.ghError(ctx, l, b, "comments", err)
		return notReusedRead
	}
	basis := reviewBasis(b, pr, comments, rules, e.promptRevision(ctx, b, l))
	if basis == "" || basis != p.Basis {
		return notReusedInputs
	}
	was, err := e.gh.CommitTree(ctx, repo, in.priorHead)
	if err != nil {
		e.ghError(ctx, l, b, "commit_tree", err)
		return notReusedRead
	}
	now, err := e.gh.CommitTree(ctx, repo, pr.HeadSHA)
	if err != nil {
		e.ghError(ctx, l, b, "commit_tree", err)
		return notReusedRead
	}
	if was != now {
		return notReusedTree
	}
	// One tree reviewed against another base is another diff.
	wasBase, err := e.gh.MergeBase(ctx, repo, b.BaseBranch, in.priorHead)
	if err != nil {
		e.ghError(ctx, l, b, "merge_base", err)
		return notReusedRead
	}
	nowBase, err := e.gh.MergeBase(ctx, repo, b.BaseBranch, pr.HeadSHA)
	if err != nil {
		e.ghError(ctx, l, b, "merge_base", err)
		return notReusedRead
	}
	if wasBase != nowBase {
		return notReusedMergeBase
	}
	commits := e.commitMessages(ctx, b, pr, in.priorHead, l)
	if commits.Unread {
		return notReusedRead
	}
	if answersFinding(commits, p.Previous(), pr.HeadSHA) {
		return notReusedCommits
	}
	in.reuse = &reuse{head: in.priorHead, task: &kagent.Task{ID: p.TaskID, Status: kagent.TaskStatus{State: kagent.StateCompleted}}}
	in.basis = basis
	// The comments equal the earlier review's (basis), so the reused report
	// carries the same decision stamps and watermark a full review would.
	in.promptVersions, in.decisionComment = decisionPrompt(b, pr, comments)
	return ""
}

// answersFinding reports a commit message since the earlier head that
// states a review response, names an earlier finding, or (on the head
// itself) carries an activation to assess. The agent weighs each of those.
func answersFinding(c Commits, prior []Finding, head string) bool {
	for _, commit := range c.List {
		msg := commit.Commit.Message
		if strings.Contains(strings.ToLower(msg), "review response") {
			return true
		}
		for _, f := range prior {
			if namesID(msg, f.ID) {
				return true
			}
		}
		if commit.SHA == head {
			if v, ok := github.ActivationTrailer(msg); ok && v != "none" {
				return true
			}
		}
	}
	return false
}

// report is the earlier head's code review as a report for this one: its
// blocking findings, reported again. Everything the agent adds on a first
// review (a summary, low findings, advisories) is gone; the blocking findings,
// the human-gate findings and the cleared record carry the verdict. The
// assessment then runs as for any report: a decision that no longer holds
// restores its finding, and the human gate, goal check, issue and Semgrep
// evidence apply to this head. A fresh copy each call, since the assessment
// edits it.
func (r *reuse) report(prior State) *Report {
	return &Report{Findings: append([]Finding{}, prior.Blocking...), Previous: []Previous{}}
}

// goalFinding reports a finding under the binding's goal check namespace.
func goalFinding(b *Binding, f Finding) bool {
	return b.ItsaplanProject != "" &&
		regexp.MustCompile(`^`+regexp.QuoteMeta(b.ItsaplanProject)+`-[1-9][0-9]*:goal(:|$)`).MatchString(f.ID)
}

// note opens the summary of a head that kept an earlier code review.
func (r *reuse) note() string {
	return fmt.Sprintf("The code review of this head is the review of %s, whose tree this head holds: no new review task ran. "+
		"The goal check, human gate, evidence and decisions were checked again for this head.\n\n", short(r.head))
}

// reuseSession is the session a reused review leaves for the next head: the
// earlier head's, now last used by this one. It adds no review, task or bytes.
func reuseSession(prev *SessionRef, pr github.PullRequest) *SessionRef {
	if prev == nil {
		return nil
	}
	ref := *prev
	ref.PR, ref.Head = pr.Number, pr.HeadSHA
	ref.Tasks = append([]string(nil), prev.Tasks...)
	return &ref
}
