package review

import (
	"crypto/rand"
	"crypto/sha256"
	"fmt"
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
	// SharedHead requires a new approval after duplicate PRs are resolved.
	// The ambiguity itself is a hold even if a later label write is lost.
	SharedHead bool `json:"shared_head,omitempty"`
	// Approver is the human whose APPROVED review of the head cleared it.
	// It is revalidated every poll, never used as a cached authorization.
	Approver string `json:"approver,omitempty"`
	// Labels stays sticky; ObservedLabels records the latest label snapshot
	// so removing and later reapplying a label creates a fresh hold. Empty
	// arrays must survive JSON. Nil identifies older states whose Labels
	// are the only available baseline.
	ObservedLabels []string `json:"observed_labels"`
	// ApprovalAfter is when a new label was observed after review started.
	// Only reviews submitted after it clear that hold. Zero preserves old
	// states and gates present when the original review started.
	ApprovalAfter time.Time `json:"approval_after,omitzero"`
	// ApprovalSnapshot records all review IDs visible in the cutoff second
	// on the first complete review read after the hold. It is persisted
	// before an ambiguous same-second approval can clear the gate.
	ApprovalSnapshot *ReviewSnapshot `json:"approval_snapshot,omitempty"`
	// ApprovalNonce lets the configured approver explicitly acknowledge a
	// hold when GitHub's second-resolution timestamp cannot order it.
	ApprovalNonce string `json:"approval_nonce,omitempty"`
	// LabelHistory identifies the complete set of durable label additions
	// already applied. Re-additions change it even when Labels stays sticky.
	LabelHistory string `json:"label_history,omitempty"`
}

// ReviewSnapshot distinguishes an observed empty second (IDs is empty)
// from missing evidence (nil snapshot/IDs or Unavailable). IDs are compared
// by membership: neither numeric order nor tie ordering is an authority.
type ReviewSnapshot struct {
	IDs         []int64 `json:"ids"`
	Unavailable bool    `json:"unavailable,omitempty"`
}

const maxSnapshotReviews = 64

func snapshotReviews(reviews []github.PullRequestReview, after time.Time) *ReviewSnapshot {
	snapshot := &ReviewSnapshot{IDs: []int64{}}
	second := after.Truncate(time.Second)
	for _, r := range reviews {
		if r.ID <= 0 || (r.SubmittedAt.IsZero() && r.State != "PENDING") {
			return &ReviewSnapshot{Unavailable: true}
		}
		if !r.SubmittedAt.Equal(second) {
			continue
		}
		if !slices.Contains(snapshot.IDs, r.ID) {
			if len(snapshot.IDs) == maxSnapshotReviews {
				return &ReviewSnapshot{Unavailable: true}
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

// observeLabelEvents replays all holds on the PR, including removed labels
// and earlier heads. The digest is independent of API ordering. We never
// infer sub-second event order from timestamps or numeric IDs.
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
	return g != nil && (g.SharedHead || len(g.Paths)+len(g.Labels)+len(g.Findings) > 0)
}

// held reports whether the service itself holds the head, by path or label.
func (g *Gate) held() bool { return g != nil && (g.SharedHead || len(g.Paths)+len(g.Labels) > 0) }

// observeLabels retains holds and fences approvals when a new label appears.
// A legacy completed state uses its sticky labels as its initial snapshot.
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
	if after.IsZero() || r.SubmittedAt.After(after) {
		return true
	}
	// GitHub exposes whole-second submitted_at values. An unseen review ID
	// from a later complete read proves a submission after our recorded
	// snapshot, even when its timestamp is the same second as the hold.
	// An approval racing between hold observation and that snapshot stays
	// excluded: its ordering cannot be established from the timestamp.
	if !r.SubmittedAt.Equal(after.Truncate(time.Second)) {
		return false
	}
	// A human can also edit an approving review to acknowledge the exact
	// persisted hold. A pre-hold review cannot predict its random token.
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
	ack := ""
	if g.ApprovalNonce != "" {
		ack = " If your approval shares the hold's timestamp second, add this exact line to that approving review: `" + approvalAcknowledgement(g.ApprovalNonce) + "`."
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
	return Outcome{Conclusion: ConclusionFailure, Output: github.CheckRunOutput{
		Title: "Split this PR",
		Summary: fmt.Sprintf("This PR has %s, not counting %d generated files. The review budget is %d lines in %d files, "+
			"and the reviewer never reviews part of a diff. Split this PR into smaller ones.\n\n%s",
			size, d.Generated, b.MaxDiffLines, b.MaxDiffFiles, footer),
	}}
}
