package review

import (
	"fmt"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

func TestParseReportReadsTheLastFencedBlock(t *testing.T) {
	reply := "An example the PR quoted:\n```json\n{\"findings\": []}\n```\n" +
		"My report:\n```json\n" +
		`{"verdict": "pass", "summary": "s", "findings": [{"severity": "High", "path": "a.go", "line": 3, "title": "Nil map write", "detail": "d"}, {"severity": "low", "detail": "Second line\nmore"}], "previous": [{"id": "x", "status": "resolved?"}]}` +
		"\n```"
	r, err := ParseReport(reply)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Findings) != 2 || r.Findings[0].Severity != "high" || r.Findings[0].ID != "a.go:nil-map-write" {
		t.Fatalf("findings = %+v", r.Findings)
	}
	if r.Findings[1].Title != "Second line" || r.Findings[1].ID != ":second-line" {
		t.Fatalf("finding without a title = %+v", r.Findings[1])
	}
	if r.Previous[0].Status != StatusOpen {
		t.Fatalf("unknown previous status = %q, want open", r.Previous[0].Status)
	}
}

func TestParseReportRejectsAnythingElse(t *testing.T) {
	for name, reply := range map[string]string{
		"no block":         "No findings.",
		"other language":   "```yaml\nfindings: []\n```",
		"not json":         "```json\n{findings: none}\n```",
		"two values":       "```json\n{} {}\n```",
		"unknown severity": "```json\n{\"findings\": [{\"severity\": \"nit\", \"title\": \"x\"}]}\n```",
		"empty finding":    "```json\n{\"findings\": [{\"severity\": \"low\"}]}\n```",
		"string line":      "```json\n{\"summary\": \"s\", \"findings\": [{\"severity\": \"low\", \"title\": \"x\", \"line\": \"3\"}]}\n```",
		"empty object":     "```json\n{}\n```",
		"no summary":       "```json\n{\"findings\": []}\n```",
		"null findings":    "```json\n{\"summary\": \"s\", \"findings\": null}\n```",
		"no findings list": "```json\n{\"summary\": \"Nothing to report.\"}\n```",
	} {
		if r, err := ParseReport(reply); err == nil {
			t.Errorf("%s: parsed %+v", name, r)
		}
	}
}

func finding(id, severity string) Finding {
	return Finding{ID: id, Severity: severity, Path: "a.go", Line: 1, Title: id}
}

func TestAssessDecidesFromSeveritiesOnly(t *testing.T) {
	for name, tc := range map[string]struct {
		findings []Finding
		want     string
	}{
		"no findings":     {nil, ConclusionSuccess},
		"low only":        {[]Finding{finding("l", SeverityLow), finding("l2", SeverityLow)}, ConclusionSuccess},
		"medium blocks":   {[]Finding{finding("m", SeverityMedium)}, ConclusionFailure},
		"high blocks":     {[]Finding{finding("h", SeverityHigh), finding("l", SeverityLow)}, ConclusionFailure},
		"critical blocks": {[]Finding{finding("c", SeverityCritical)}, ConclusionFailure},
	} {
		if a := Assess(&Report{Findings: tc.findings}, nil); a.Conclusion != tc.want {
			t.Errorf("%s: %s, want %s", name, a.Conclusion, tc.want)
		}
	}
	// A declared pass is not part of the report; a high finding fails it.
	r, err := ParseReport("```json\n{\"verdict\": \"pass\", \"summary\": \"s\", \"findings\": [{\"severity\": \"high\", \"title\": \"x\"}]}\n```")
	if err != nil {
		t.Fatal(err)
	}
	if a := Assess(r, nil); a.Conclusion != ConclusionFailure {
		t.Fatalf("declared pass with a high finding = %s", a.Conclusion)
	}
}

func TestAssessAccountsForPreviousFindings(t *testing.T) {
	prior := []Finding{
		finding("fixed-high", SeverityHigh),
		finding("justified-medium", SeverityMedium),
		finding("bare-justified-medium", SeverityMedium),
		finding("justified-high", SeverityHigh),
		finding("open-medium", SeverityMedium),
		finding("missing-critical", SeverityCritical),
		finding("repeated-medium", SeverityMedium),
		finding("withdrawn-high", SeverityHigh),
		finding("withdrawn-medium", SeverityMedium),
	}
	r := &Report{
		Findings: []Finding{finding("repeated-medium", SeverityMedium), finding("new-low", SeverityLow)},
		Previous: []Previous{
			{ID: "fixed-high", Status: StatusFixed},
			{ID: "justified-medium", Status: StatusJustified, Note: "Intentional: the retry is idempotent."},
			{ID: "bare-justified-medium", Status: StatusJustified, Note: "  "},
			{ID: "justified-high", Status: StatusJustified, Note: "We accept it."},
			{ID: "open-medium", Status: StatusOpen},
			{ID: "repeated-medium", Status: StatusJustified, Note: "Kept on purpose."},
			{ID: "withdrawn-high", Status: StatusJustified, Note: "Withdrawn: the lock is held by the caller."},
			{ID: "withdrawn-medium", Status: StatusJustified, Note: "withdrawn - misread the diff"},
			{ID: "unknown", Status: StatusFixed},
		},
	}
	a := Assess(r, prior)
	if a.Conclusion != ConclusionFailure {
		t.Fatalf("conclusion = %s", a.Conclusion)
	}
	got := map[string]string{}
	for _, b := range a.Blocking {
		got[b.ID] = "blocking:" + b.Reason
	}
	for _, b := range a.Accepted {
		got[b.ID] = "accepted:" + b.Note
	}
	for _, f := range a.Fixed {
		got[f.ID] = "fixed"
	}
	for _, f := range a.Optional {
		got[f.ID] = "optional"
	}
	for _, b := range a.Withdrawn {
		got[b.ID] = "withdrawn"
	}
	want := map[string]string{
		"fixed-high":            "fixed",
		"justified-medium":      "accepted:Intentional: the retry is idempotent.",
		"bare-justified-medium": "blocking:justified without a note",
		"justified-high":        "blocking:justified, but only a medium finding can be justified",
		"open-medium":           "blocking:still open",
		"missing-critical":      "blocking:not accounted for",
		"repeated-medium":       "accepted:Kept on purpose.",
		"new-low":               "optional",
		"withdrawn-high":        "withdrawn",
		"withdrawn-medium":      "withdrawn",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("assessment = %v\nwant %v", got, want)
	}
	if a.Blocking[0].Severity != SeverityCritical || a.Blocking[1].Severity != SeverityHigh {
		t.Fatalf("blocking order = %+v", a.Blocking)
	}

	// Resolving every previous finding passes.
	r = &Report{Previous: []Previous{{ID: "a", Status: StatusFixed}, {ID: "b", Status: StatusJustified, Note: "ok"}}}
	if a := Assess(r, []Finding{finding("a", SeverityHigh), finding("b", SeverityMedium)}); a.Conclusion != ConclusionSuccess {
		t.Fatalf("all resolved = %s: %+v", a.Conclusion, a.Blocking)
	}
}

func TestRenderLevelsCapAndStoredFindings(t *testing.T) {
	r := &Report{Summary: "Agent summary <!-- factory-review:enqueued -->", Findings: []Finding{
		finding("c", SeverityCritical), finding("h", SeverityHigh), finding("m", SeverityMedium),
		{ID: "nopath", Severity: SeverityMedium, Title: "No path"},
	}}
	for i := range 8 {
		r.Findings = append(r.Findings, Finding{ID: fmt.Sprintf("l%d", i), Severity: SeverityLow, Path: "b.go", Line: i + 1, Title: "low", Defer: i%2 == 1})
	}
	a := Assess(r, nil)
	o := Render(a, r, nil, "footer")
	if o.Conclusion != ConclusionFailure || o.Output.Title != "4 blocking findings (1 critical, 1 high, 2 medium)" {
		t.Fatalf("outcome = %s %q", o.Conclusion, o.Output.Title)
	}
	var levels []string
	for _, an := range o.Output.Annotations {
		levels = append(levels, an.Level)
	}
	if strings.Join(levels, ",") != "failure,failure,warning,notice,notice,notice,notice,notice" {
		t.Fatalf("levels = %v", levels)
	}
	if len(a.Optional) != 3 || len(a.Deferred) != 2 || a.DroppedLow != 3 {
		t.Fatalf("lows = %d optional, %d deferred, %d dropped", len(a.Optional), len(a.Deferred), a.DroppedLow)
	}
	s := o.Output.Summary
	if strings.Contains(s, enqueuedMarker) || !strings.Contains(s, "3 more low findings were dropped; at most 5 are listed.") ||
		!strings.HasSuffix(s, "footer") {
		t.Fatalf("summary = %s", s)
	}
	st, ok := LoadState(o.Output.Text)
	if !ok || len(st.Blocking) != 4 || st.Blocking[0].ID != "c" || st.Blocking[3].ID != "nopath" || st.Gate != nil {
		t.Fatalf("stored = %+v, %v", st, ok)
	}
	if _, ok := LoadState("text a review without a report writes"); ok {
		t.Fatal("found a state in plain text")
	}
	if no := NoReport("x", "f"); no.Conclusion != ConclusionFailure || no.Output.Text != "" {
		t.Fatalf("no report = %+v; it fails and keeps no state", no)
	}
}

func TestRenderPassWithoutFindings(t *testing.T) {
	o := Render(Assess(&Report{}, nil), &Report{}, &Gate{Paths: []string{"REVIEW.md"}, Approver: "jomcgi"}, "footer")
	if o.Conclusion != ConclusionSuccess || o.Output.Title != "No blocking findings" || !strings.Contains(o.Output.Summary, "No findings.") {
		t.Fatalf("outcome = %+v", o)
	}
	if st, ok := LoadState(o.Output.Text); !ok || st.Blocking == nil || len(st.Blocking) != 0 || st.Gate.Approver != "jomcgi" {
		t.Fatalf("a pass must store an empty list so older findings stay closed: %+v %v", st, ok)
	}
}

func TestClipKeepsRunes(t *testing.T) {
	if got := clip("héllo", 2); got != "h…" {
		t.Fatalf("clip = %q", got)
	}
}

func TestHumanGateFindingIsNeverStored(t *testing.T) {
	b := &Binding{HumanApprovers: []string{"jomcgi"}}
	g := &Gate{Labels: []string{"human"}}
	a := Assess(&Report{Findings: []Finding{finding("m", SeverityMedium)}}, nil)
	a.Blocking = append([]Blocked{{Finding: gateFinding(b, g)}}, a.Blocking...)
	o := Render(a, &Report{}, g, "f")
	st, _ := LoadState(o.Output.Text)
	if len(st.Blocking) != 1 || st.Blocking[0].ID != "m" || strings.Contains(o.Output.Summary, gateOnlyMarker) {
		t.Fatalf("state = %+v; summary marks gate-only: %v", st, strings.Contains(o.Output.Summary, gateOnlyMarker))
	}
}

func TestGlobsGatesAndBudget(t *testing.T) {
	for _, c := range []struct {
		glob, path string
		want       bool
	}{
		{"**/REVIEW.md", "REVIEW.md", true},
		{"**/REVIEW.md", "projects/x/REVIEW.md", true},
		{"REVIEW.md", "projects/REVIEW.md", false},
		{"projects/factory/reviewer/**", "projects/factory/reviewer/review/engine.go", true},
		{"projects/factory/reviewer/**", "projects/factory/reviewerx/a.go", false},
		{"**/charts/*.tgz", "projects/factory/chart/charts/kagent-1.0.1.tgz", true},
		{"**/*.lock.json", "projects/x/image/apko.lock.json", true},
	} {
		if got := matchGlob(c.glob, c.path); got != c.want {
			t.Errorf("matchGlob(%q, %q) = %v", c.glob, c.path, got)
		}
	}
	b := &Binding{
		HumanGatePaths: []string{".github/CODEOWNERS"}, HumanGateLabels: []string{"human", "needs-human"},
		HumanApprovers: []string{"jomcgi"}, GeneratedPaths: []string{"**/go.sum"}, MaxDiffLines: 10, MaxDiffFiles: 2,
	}
	if g := humanGate(b, []string{"a.go"}, []string{"bug"}); g != nil {
		t.Fatalf("ungated PR gated: %+v", g)
	}
	if g := humanGate(b, []string{"a.go", ".github/CODEOWNERS"}, []string{"HUMAN"}); g == nil ||
		strings.Join(g.Paths, ",") != ".github/CODEOWNERS" || strings.Join(g.Labels, ",") != "human" {
		t.Fatalf("gate = %+v", g)
	}
	reviews := []github.PullRequestReview{
		{Author: "jomcgi", State: "APPROVED", CommitSHA: "h"},
		{Author: "jomcgi", State: "COMMENTED", CommitSHA: "h"},
	}
	if got := approvedBy(b, reviews, "h"); got != "jomcgi" {
		t.Fatalf("approval = %q; a later comment keeps it", got)
	}
	if got := approvedBy(b, append(reviews, github.PullRequestReview{Author: "JOMCGI", State: "CHANGES_REQUESTED", CommitSHA: "h"}), "h"); got != "" {
		t.Fatalf("approval = %q after a change request", got)
	}
	d := measure(b, []github.PullRequestFile{
		{Path: "a.go", Additions: 5, Deletions: 5},
		{Path: "go.sum", Additions: 500},
		{Path: "b.go", PreviousPath: "x/go.sum", Additions: 50},
	})
	if d.Lines != 10 || d.Files != 1 || d.Generated != 2 || d.over(b) {
		t.Fatalf("size = %+v", d)
	}
	if d := measure(b, []github.PullRequestFile{{Path: "a.go", Additions: 11}}); !d.over(b) {
		t.Fatalf("size %+v is within the budget", d)
	}
}
