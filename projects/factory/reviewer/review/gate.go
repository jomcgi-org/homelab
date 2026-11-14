package review

import (
	"fmt"
	"path"
	"sort"
	"strings"

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
	// Approver is the human whose APPROVED review of the head cleared it.
	Approver string `json:"approver,omitempty"`
}

// Needed reports whether the gate applies.
func (g *Gate) Needed() bool { return g != nil && len(g.Paths)+len(g.Labels)+len(g.Findings) > 0 }

// held reports whether the service itself holds the head, by path or label.
func (g *Gate) held() bool { return g != nil && len(g.Paths)+len(g.Labels) > 0 }

// humanGate finds the gated paths among a head's changed paths (both sides
// of the diff) and the gate labels on its PR.
func humanGate(b *Binding, paths, labels []string) *Gate {
	g := &Gate{}
	seen := map[string]bool{}
	for _, p := range paths {
		if c := cleanPath(p); c != "" && !seen[c] && matchAny(b.HumanGatePaths, c) {
			seen[c] = true
			g.Paths = append(g.Paths, c)
		}
	}
	sort.Strings(g.Paths)
	if len(g.Paths) > maxGatePaths {
		g.Paths = g.Paths[:maxGatePaths]
	}
	g.Labels = gateLabels(b, labels)
	if !g.held() {
		return nil
	}
	return g
}

func gateLabels(b *Binding, labels []string) []string {
	var out []string
	for _, l := range labels {
		for _, gl := range b.HumanGateLabels {
			if strings.EqualFold(l, gl) {
				out = append(out, gl)
			}
		}
	}
	return out
}

// approvedBy is the configured human whose latest decision on the PR is an
// APPROVED review of exactly head, or empty.
func approvedBy(b *Binding, reviews []github.PullRequestReview, head string) string {
	latest := map[string]github.PullRequestReview{}
	for _, r := range reviews { // oldest first
		switch r.State {
		case "APPROVED", "CHANGES_REQUESTED", "DISMISSED":
			latest[strings.ToLower(r.Author)] = r
		}
	}
	for _, a := range b.HumanApprovers {
		if r, ok := latest[strings.ToLower(a)]; ok && r.State == "APPROVED" && r.CommitSHA == head {
			return a
		}
	}
	return ""
}

// gateFinding is the high finding the service adds while a gated head
// waits for its human.
func gateFinding(b *Binding, g *Gate) Finding {
	var why []string
	if len(g.Paths) > 0 {
		why = append(why, "touches "+strings.Join(g.Paths, ", "))
	}
	if len(g.Labels) > 0 {
		why = append(why, "carries "+strings.Join(g.Labels, ", "))
	}
	return Finding{
		ID: HumanGateID, Severity: SeverityHigh,
		Title: "Needs an approving review from " + strings.Join(b.HumanApprovers, " or ") + " on this head",
		Detail: fmt.Sprintf("This PR %s. Only an APPROVED review of this exact commit by %s clears it; no revision round fixes it.",
			strings.Join(why, " and "), strings.Join(b.HumanApprovers, " or ")),
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

// measure sizes the diff, leaving out files whose path on either side is
// generated.
func measure(b *Binding, files []github.PullRequestFile) DiffSize {
	var d DiffSize
	for _, f := range files {
		if matchAny(b.GeneratedPaths, f.Path) || (f.PreviousPath != "" && matchAny(b.GeneratedPaths, f.PreviousPath)) {
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
