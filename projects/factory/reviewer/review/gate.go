package review

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/mail"
	"path"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

// matchGlob reports whether p matches pattern. Segments match with
// path.Match; a ** segment matches any number of directories, none
// included.
func matchGlob(pattern, p string) bool {
	return matchSegs(strings.Split(pattern, "/"), strings.Split(p, "/"))
}

func matchSegs(pat, segs []string) bool {
	for len(pat) > 0 {
		if pat[0] == "**" {
			for i := 0; i <= len(segs); i++ {
				if matchSegs(pat[1:], segs[i:]) {
					return true
				}
			}
			return false
		}
		if len(segs) == 0 {
			return false
		}
		if ok, _ := path.Match(pat[0], segs[0]); !ok {
			return false
		}
		pat, segs = pat[1:], segs[1:]
	}
	return len(segs) == 0
}

func matchAny(patterns []string, p string) bool {
	for _, g := range patterns {
		if matchGlob(g, p) {
			return true
		}
	}
	return false
}

// HumanGateID is the ID of the finding the service adds for the human gate.
const HumanGateID = "human-gate"

// maxGatePaths bounds the gated paths a check run lists and stores.
const maxGatePaths = 20

// Gate is why a head needs a human: the gated paths it touches, the gate
// labels its PR carries and the human-gate findings the agent reported.
type Gate struct {
	Paths    []string  `json:"paths,omitempty"`
	Labels   []string  `json:"labels,omitempty"`
	Findings []Finding `json:"findings,omitempty"`
	// SharedHead makes a recorded duplicate itself a sticky human hold.
	SharedHead    bool   `json:"shared_head,omitempty"`
	RecoveryEpoch string `json:"recovery_epoch,omitempty"`
	RecoveryPR    int    `json:"recovery_pr,omitempty"`
	ContextToken  string `json:"context_token,omitempty"`
	// Approver is the human whose APPROVED review of the head cleared it.
	// It is revalidated every poll, never used as a cached authorization.
	Approver string `json:"approver,omitempty"`
	// Authorship is current evidence, never persisted or inherited as approval.
	Authorship string `json:"-"`
	// ObservedLabels detects re-additions; nil means a legacy state using sticky Labels as its baseline.
	ObservedLabels   []string        `json:"observed_labels"`
	ApprovalAfter    time.Time       `json:"approval_after,omitzero"`     // ApprovalAfter fences new holds; zero preserves legacy/initial gates.
	ApprovalSnapshot *ReviewSnapshot `json:"approval_snapshot,omitempty"` // ApprovalSnapshot records the first complete review read after a hold.
	ApprovalNonce    string          `json:"approval_nonce,omitempty"`    // ApprovalNonce allows authenticated acknowledgement of ambiguous timing.
	LabelHistory     string          `json:"label_history,omitempty"`     // LabelHistory identifies applied additions, including repeated labels.
}

// Snapshot membership supplies ordering; nil/Unavailable is unknown, empty IDs is known empty.
type ReviewSnapshot struct {
	IDs         []int64 `json:"ids"`
	Unavailable bool    `json:"unavailable,omitempty"`
	All         bool    `json:"all,omitempty"` // Recovery uses causal order, including when clocks differ.
}

const maxSnapshotReviews = 64

func snapshotReviews(reviews []github.PullRequestReview, after time.Time, all bool) *ReviewSnapshot {
	snapshot := &ReviewSnapshot{IDs: []int64{}, All: all}
	second := after.Truncate(time.Second)
	for _, r := range reviews {
		if r.ID <= 0 || (r.SubmittedAt.IsZero() && r.State != "PENDING") {
			return &ReviewSnapshot{Unavailable: true, All: all}
		}
		if !all && !r.SubmittedAt.Equal(second) {
			continue
		}
		if !slices.Contains(snapshot.IDs, r.ID) {
			if len(snapshot.IDs) == maxSnapshotReviews {
				return &ReviewSnapshot{Unavailable: true, All: all}
			}
			snapshot.IDs = append(snapshot.IDs, r.ID)
		}
	}
	return snapshot
}

func (g *Gate) resetApproval(now time.Time) {
	g.ApprovalAfter, g.Approver, g.ApprovalSnapshot = now.UTC(), "", nil
	g.ApprovalNonce = rand.Text()
}

// observeLabelEvents replays removed and older-head holds without ordering ties.
func (g *Gate) observeLabelEvents(b *Binding, events []github.LabelEvent) bool {
	var records []string
	var latest time.Time
	for _, event := range events {
		labels := gateLabels(b, []string{event.Label})
		if len(labels) == 0 {
			continue
		}
		label := labels[0]
		records = append(records, fmt.Sprintf("%d:%s:%s", event.ID, label, event.CreatedAt.UTC().Format(time.RFC3339Nano)))
		if event.CreatedAt.After(latest) {
			latest = event.CreatedAt
		}
		if !slices.Contains(g.Labels, label) {
			g.Labels = append(g.Labels, label)
		}
	}
	if len(records) == 0 && g.LabelHistory == "" {
		return false
	}
	sort.Strings(records)
	digest := fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(slices.Compact(records), "\n"))))
	if digest == g.LabelHistory {
		return false
	}
	g.LabelHistory = digest
	if latest.Before(g.ApprovalAfter) {
		latest = g.ApprovalAfter // Keep a later duplicate/observed-label hold.
	}
	g.resetApproval(latest)
	return true
}

// Needed reports whether the gate applies.
func (g *Gate) Needed() bool {
	return g != nil && (g.held() || len(g.Findings) > 0)
}

func (g *Gate) approved() bool { return g.Approver != "" || g.Authorship != "" }

// reviewRequired holds cannot be cleared by authorship, even on Joe's PRs.
func (g *Gate) reviewRequired() bool {
	return g.SharedHead || g.RecoveryEpoch != "" || len(g.Labels) > 0 || !g.ApprovalAfter.IsZero()
}

const (
	defaultRenovateEmail = "renovate@whitesourcesoftware.com"
	authorshipPrefix     = "Joe's approval by authorship: "
)

// authorshipApproval recomputes the waiver from GitHub on every evaluation.
// Renovate's identity comes from trusted base config, never the PR's version.
func (e *Engine) authorshipApproval(ctx context.Context, b *Binding, pr github.PullRequest) (string, error) {
	if pr.Author == "" || !containsFold(b.HumanApprovers, pr.Author) || pr.HeadRepo == "" || !strings.EqualFold(pr.HeadRepo, b.repo()) ||
		pr.HeadRef == "" || pr.HeadSHA == "" || strings.HasPrefix(strings.ToLower(pr.HeadRef), "renovate/") {
		return "", nil
	}
	base, err := e.gh.BranchHead(ctx, b.repo(), b.BaseBranch)
	if err != nil {
		return "", err
	}
	config, err := e.gh.FileContent(ctx, b.repo(), "renovate.json", base)
	if err != nil && !errors.Is(err, github.ErrNotFound) {
		return "", err
	}
	renovateEmail := defaultRenovateEmail
	if err == nil {
		var cfg struct {
			GitAuthor string `json:"gitAuthor"`
		}
		if err := json.Unmarshal(config, &cfg); err != nil {
			return "", err
		}
		if cfg.GitAuthor != "" {
			author, err := mail.ParseAddress(cfg.GitAuthor)
			if err != nil {
				return "", err
			}
			renovateEmail = author.Address
		}
	}
	commits, err := e.gh.PullRequestCommits(ctx, b.repo(), pr.Number, pr.HeadSHA)
	if err != nil {
		return "", err
	}
	for _, cm := range commits {
		if strings.EqualFold(cm.Commit.Author.Email, renovateEmail) || strings.EqualFold(cm.Commit.Author.Email, defaultRenovateEmail) {
			return "", nil
		}
	}
	events, err := e.gh.HeadRefActivity(ctx, b.repo(), pr.HeadRef)
	if err != nil {
		return "", err
	}
	return approvalByPushes(b, pr, events), nil
}

func approvalByPushes(b *Binding, pr github.PullRequest, events []github.RefActivity) string {
	if len(events) == 0 || len(events) > github.MaxListed {
		return ""
	}
	pushes := 0
	actors := []string{}
	expected := pr.HeadSHA
	for _, event := range events {
		if event.Ref != "refs/heads/"+pr.HeadRef || event.Actor.Login == "" || !containsFold(b.HumanApprovers, event.Actor.Login) || event.After == "" {
			return ""
		}
		switch event.Type {
		case "push", "force_push", "pr_merge", "merge_queue_merge":
			if event.After != expected || event.Before == "" {
				return "" // A newer head or a gap between pushes.
			}
			expected = event.Before
			pushes++
			if !containsFold(actors, event.Actor.Login) {
				actors = append(actors, event.Actor.Login)
			}
		case "branch_creation":
			if event.After != expected {
				return ""
			}
			// Creation is the first push to this incarnation of the ref.
			pushes++
			if !containsFold(actors, event.Actor.Login) {
				actors = append(actors, event.Actor.Login)
			}
			sort.Strings(actors)
			return fmt.Sprintf("%sauthor %s, %d %s, all by %s.", authorshipPrefix, pr.Author, pushes, plural(pushes, "push", "pushes"), strings.Join(actors, ", "))
		default:
			return "" // Deletion or unknown activity cannot prove an unbroken ref.
		}
	}
	return ""
}

// held reports whether the service itself holds the head, by path or label.
func (g *Gate) held() bool {
	return g != nil && (g.SharedHead || g.RecoveryEpoch != "" || len(g.Paths)+len(g.Labels) > 0)
}

// inherit carries service holds, never a cached old-head approver.
func (g *Gate) inherit(prior *Gate) {
	if prior.ContextToken != "" {
		g.ContextToken = prior.ContextToken
	}
	g.SharedHead = g.SharedHead || prior.SharedHead
	for _, label := range prior.Labels {
		if !slices.Contains(g.Labels, label) {
			g.Labels = append(g.Labels, label)
		}
	}
	newRecovery := prior.RecoveryEpoch != "" && (prior.RecoveryEpoch != g.RecoveryEpoch || prior.RecoveryPR != g.RecoveryPR)
	if prior.RecoveryEpoch != "" {
		g.RecoveryEpoch, g.RecoveryPR = prior.RecoveryEpoch, prior.RecoveryPR
	}
	if newRecovery || prior.ApprovalAfter.After(g.ApprovalAfter) {
		g.ApprovalAfter, g.ApprovalNonce, g.ApprovalSnapshot = prior.ApprovalAfter, prior.ApprovalNonce, prior.ApprovalSnapshot
	}
}

// observeLabels retains holds and detects new labels against the last snapshot.
func (g *Gate) observeLabels(labels []string, now time.Time) bool {
	observed := g.ObservedLabels
	if observed == nil {
		observed = g.Labels
	}
	changed := !slices.Equal(observed, labels)
	for _, lb := range labels {
		if !slices.Contains(observed, lb) {
			g.resetApproval(now)
		}
		if !slices.Contains(g.Labels, lb) {
			g.Labels = append(g.Labels, lb)
		}
	}
	g.ObservedLabels = labels
	return changed
}

// humanGate finds the gated paths among a head's changed paths (both sides
// of the diff) and the gate labels on its PR. A path of any characters is
// matched as it is; one with an empty or ".." segment, which no rule can be
// looked up for, is gated.
func humanGate(b *Binding, paths, labels []string) *Gate {
	g := &Gate{}
	seen := map[string]bool{}
	for _, p := range paths {
		c, ok := lookupPath(p)
		if !ok {
			c = p
		}
		if !seen[c] && (!ok || matchAny(b.HumanGatePaths, c)) {
			seen[c] = true
			g.Paths = append(g.Paths, c)
		}
	}
	sort.Strings(g.Paths)
	if len(g.Paths) > maxGatePaths {
		g.Paths = g.Paths[:maxGatePaths]
	}
	g.Labels = gateLabels(b, labels)
	g.ObservedLabels = g.Labels
	if !g.held() {
		return nil
	}
	return g
}

func gateLabels(b *Binding, labels []string) []string {
	out := []string{}
	for _, l := range labels {
		for _, gl := range b.HumanGateLabels {
			if strings.EqualFold(l, gl) {
				out = append(out, gl)
			}
		}
	}
	sort.Strings(out)
	return slices.Compact(out)
}

// approvedBy is the configured human whose latest decision on the PR is an
// APPROVED review of exactly head, submitted after a nonzero cutoff, or empty.
func approvedBy(b *Binding, reviews []github.PullRequestReview, head string, after time.Time, snapshot *ReviewSnapshot, acknowledged map[int64]bool) string {
	latest := latestDecisions(reviews)
	for _, a := range b.HumanApprovers {
		if r, ok := latest[strings.ToLower(a)]; ok && r.State == "APPROVED" && r.CommitSHA == head &&
			approvalAfter(r, after, snapshot, acknowledged[r.ID]) {
			return a
		}
	}
	return ""
}

func latestDecisions(reviews []github.PullRequestReview) map[string]github.PullRequestReview {
	latest := map[string]github.PullRequestReview{}
	for _, r := range reviews { // oldest first
		switch r.State {
		case "APPROVED", "CHANGES_REQUESTED", "DISMISSED":
			latest[strings.ToLower(r.Author)] = r
		}
	}
	return latest
}

func approvalAfter(r github.PullRequestReview, after time.Time, snapshot *ReviewSnapshot, acknowledged bool) bool {
	if snapshot != nil && snapshot.All {
		return r.ID > 0 && (acknowledged || (snapshot.IDs != nil && !snapshot.Unavailable && !slices.Contains(snapshot.IDs, r.ID)))
	}
	if after.IsZero() || r.SubmittedAt.After(after) {
		return true
	}
	// Whole-second timestamps need a new post-snapshot ID or explicit acknowledgement.
	if !r.SubmittedAt.Equal(after.Truncate(time.Second)) {
		return false
	}
	// A pre-hold review cannot predict the acknowledgement token.
	if r.ID > 0 && acknowledged {
		return true
	}
	return r.ID > 0 && snapshot != nil && snapshot.IDs != nil && !snapshot.Unavailable && !slices.Contains(snapshot.IDs, r.ID)
}

func approvalAcknowledgement(nonce string) string { return "factory-review-ack:" + nonce }

func acknowledges(body, nonce string) bool {
	return nonce != "" && slices.Contains(strings.Split(strings.ReplaceAll(body, "\r\n", "\n"), "\n"), approvalAcknowledgement(nonce))
}

// gateFinding is the high finding the service adds while a gated head
// waits for its human.
func gateFinding(b *Binding, g *Gate) Finding {
	var why []string
	if g.RecoveryEpoch != "" {
		why = append(why, "needs its context confirmed after reviewer recovery")
	}
	if g.SharedHead {
		why = append(why, "shared its head with another ready PR")
	}
	if len(g.Paths) > 0 {
		quoted := make([]string, len(g.Paths))
		for i, p := range g.Paths {
			quoted[i] = quotePath(clip(p, maxStoredPath))
		}
		why = append(why, "touches "+strings.Join(quoted, ", "))
	}
	if len(g.Labels) > 0 {
		why = append(why, "carries "+strings.Join(g.Labels, ", "))
	}
	if len(g.Findings) > 0 {
		why = append(why, "has unresolved human-gate findings")
	}
	fresh := ""
	if !g.ApprovalAfter.IsZero() {
		fresh = " submitted after the hold was observed at " + g.ApprovalAfter.UTC().Format(time.RFC3339Nano)
	}
	if g.RecoveryEpoch != "" {
		fresh = " first seen after this hold's recorded review baseline"
	}
	ack := ""
	if g.ApprovalNonce != "" {
		ack = " If your approval predates this check or shares its timestamp second, acknowledge this hold with this exact line in that approving review: `" + approvalAcknowledgement(g.ApprovalNonce) + "`."
	}
	return Finding{
		ID: HumanGateID, Severity: SeverityHigh,
		Title: "Needs an approving review from " + strings.Join(b.HumanApprovers, " or ") + " on this head",
		Detail: fmt.Sprintf("Only an APPROVED review of this exact commit by %s%s clears this hold.%s This PR %s; no revision round fixes it.",
			strings.Join(b.HumanApprovers, " or "), fresh, ack, strings.Join(why, " and ")),
	}
}

// DiffSize is a PR's size without its generated files.
type DiffSize struct {
	Lines     int
	Files     int
	Generated int
	// Capped is set when GitHub's file listing was cut off.
	Capped bool
}

// measure sizes the diff, leaving out files generated at their new path: a
// file renamed away from a generated path is reviewed like any other.
func measure(b *Binding, files []github.PullRequestFile) DiffSize {
	var d DiffSize
	for _, f := range files {
		if matchAny(b.GeneratedPaths, f.Path) {
			d.Generated++
			continue
		}
		d.Files++
		d.Lines += f.Additions + f.Deletions
	}
	d.Capped = len(files) >= github.MaxFilePages*github.MaxListed
	return d
}

// over reports whether the diff exceeds the binding's budget.
func (d DiffSize) over(b *Binding) bool {
	return d.Capped || d.Lines > b.MaxDiffLines || d.Files > b.MaxDiffFiles
}

// SplitOutcome fails a PR over the diff budget; no task is sent for it.
func SplitOutcome(b *Binding, d DiffSize, footer string) Outcome {
	size := fmt.Sprintf("%d changed lines in %d files", d.Lines, d.Files)
	if d.Capped {
		size = "more files than GitHub lists"
	}
	return Outcome{
		Conclusion: ConclusionFailure,
		Output: github.CheckRunOutput{Title: "Split this PR", Summary: fmt.Sprintf(
			"This PR has %s, not counting %d generated files. The review budget is %d lines in %d files, "+
				"and the reviewer never reviews part of a diff. Split this PR into smaller ones.\n\n%s",
			size, d.Generated, b.MaxDiffLines, b.MaxDiffFiles, footer)},
		Comment: &CommentUpdate{
			Verdict: VerdictChanges, Headline: "Split this PR",
			Note: fmt.Sprintf("%s; the budget is %d lines in %d files.", size, b.MaxDiffLines, b.MaxDiffFiles),
		},
	}
}
