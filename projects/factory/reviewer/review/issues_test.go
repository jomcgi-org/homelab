package review

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

type issueCommits struct {
	GitHub
	messages []string
	err      error
	calls    int
}

func (g *issueCommits) PullRequestCommits(context.Context, string, int, string) ([]github.PullRequestCommit, error) {
	g.calls++
	out := make([]github.PullRequestCommit, len(g.messages))
	for i, m := range g.messages {
		out[i].Commit.Message = m
	}
	return out, g.err
}

func TestIssueResolution(t *testing.T) {
	b := &Binding{ItsaplanProject: "HOME", BranchPrefix: "factory/"}
	for _, tc := range []struct {
		name, author, branch, body, want string
		messages                         []string
		calls                            int
		fails                            bool
	}{
		{"factory branch wins", "jomcgi-factory[bot]", "factory/HOME-58", "Refs HOME-99", "HOME-58", []string{"Refs HOME-100"}, 0, false},
		{"invalid factory branch cannot override", "jomcgi-factory[bot]", "factory/HOME-58/other", "Refs HOME-99", "", nil, 0, true},
		{"body first", "jomcgi", "factory/HOME-58", "Refs HOME-12\nRefs HOME-13", "HOME-12", []string{"Refs HOME-14"}, 0, false},
		{"commits first", "renovate[bot]", "update", "no reference", "HOME-15", []string{"plain", "change\nRefs HOME-15", "Refs HOME-16"}, 1, false},
		{"no issue", "jomcgi", "branch", "Refs OTHER-1", "", []string{"no reference"}, 1, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gh := &issueCommits{messages: tc.messages}
			e := &Engine{gh: gh}
			pr := github.PullRequest{Author: tc.author, HeadRef: tc.branch, Body: tc.body, HeadSHA: shaA}
			for range 2 { // The second read of the head reuses the first.
				key, err := e.resolveIssue(context.Background(), b, pr)
				if key != tc.want || (err != nil) != tc.fails || gh.calls != tc.calls {
					t.Fatalf("resolution = %q, %v, %d calls", key, err, gh.calls)
				}
			}
		})
	}
	// Over GitHub's 250-commit list nothing can be read: the author must
	// name the issue. Any other read error is retried.
	for _, err := range []error{github.ErrTooMany, context.DeadlineExceeded} {
		e := &Engine{cfg: Config{MACSecret: testMACSecret}, gh: &issueCommits{err: err}}
		in := e.issueContext(context.Background(), &Binding{ItsaplanProject: "HOME", HumanApprovers: []string{"jomcgi"}}, github.PullRequest{Author: "jomcgi", HeadSHA: shaA}, nil)
		permanent := errors.Is(err, github.ErrTooMany)
		if (len(in.Findings) == 1) != permanent || (len(in.Pending) == 1) == permanent {
			t.Fatalf("commit read %v classified as %+v", err, in)
		}
	}
}

type issueAPI struct {
	project                           *itsaplan.Project
	issue                             *itsaplan.Issue
	author                            string
	textAuthors                       []string
	comments                          map[int64]*itsaplan.FeedItem
	fail                              string
	issueReads, commentReads, lookups int
}

func (a *issueAPI) GetProject(context.Context, string) (*itsaplan.Project, error) {
	if a.fail == "project" {
		return nil, errors.New("private API failure body")
	}
	return a.project, nil
}

func (a *issueAPI) GetIssueByNumber(context.Context, string, int64) (*itsaplan.Issue, error) {
	a.issueReads++
	if a.fail == "issue" {
		return nil, errors.New("private API failure body")
	}
	if a.fail == "missing" {
		return nil, itsaplan.ErrNotFound
	}
	return a.issue, nil
}

func (a *issueAPI) FindIssue(_ context.Context, key string) (*itsaplan.Issue, error) {
	a.lookups++
	i := *a.issue
	i.Identifier, i.Description = key, ""
	return &i, nil
}

func (a *issueAPI) IssueAuthor(context.Context, int64) (string, error) {
	if a.fail == "author" {
		return "", errors.New("private API failure body")
	}
	return a.author, nil
}

func (a *issueAPI) IssueTextAuthors(context.Context, *itsaplan.Issue) ([]string, error) {
	if a.fail == "history" {
		return nil, errors.New("private API failure body")
	}
	return a.textAuthors, nil
}

func (a *issueAPI) LinkedComment(ctx context.Context, _ int64, id int64) (*itsaplan.FeedItem, error) {
	a.commentReads++
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if a.fail == "comment" {
		return nil, errors.New("private API failure body")
	}
	if a.comments[id] == nil {
		return nil, itsaplan.ErrNotFound
	}
	return a.comments[id], nil
}

// testMACSecret stands in for the mounted itsaplan key the MAC key derives from.
func testMACSecret() (string, error) { return "test-itsaplan-key-0123456789", nil }

func issueSetup() (*Engine, *Binding, *issueAPI, github.PullRequest) {
	p := &itsaplan.Project{
		Columns: []itsaplan.Column{{ID: 2, Name: "In progress"}},
		Assignees: []itsaplan.Assignee{
			{UserID: "human", Username: "joe", Kind: "member"},
			{UserID: "sessions", Username: "joesessions", Kind: "member"},
			{UserID: "bot", Username: "joe", Kind: "agent"},
			{UserID: "admin", Username: "factoryadmin", Kind: "member"},
		},
	}
	p.Project.ID, p.Project.Key = 3, "HOME"
	a := &issueAPI{
		project: p, author: "human", textAuthors: []string{"human", "human"},
		issue:    &itsaplan.Issue{ID: 95, ProjectID: 3, Identifier: "HOME-58", Title: "Read the issue", Description: "Report unmet criteria", ColumnID: 2},
		comments: map[int64]*itsaplan.FeedItem{},
	}
	e := &Engine{cfg: Config{Itsaplan: a, MACSecret: testMACSecret}, gh: &issueCommits{}}
	b := &Binding{ItsaplanProject: "HOME", HumanApprovers: []string{"jomcgi"}, TrustedItsaplanAuthors: []string{"joe", "joesessions", "factoryadmin"}}
	pr := github.PullRequest{Author: "jomcgi-factory[bot]", HeadRef: "factory/HOME-58", HeadSHA: shaA}
	return e, b, a, pr
}

func TestIssueAuthority(t *testing.T) {
	for _, tc := range []struct {
		author  string
		trusted bool
	}{
		{"human", true}, {"sessions", true}, {"bot", false}, {"admin", false}, {"deleted", false},
	} {
		t.Run(tc.author, func(t *testing.T) {
			e, b, api, pr := issueSetup()
			api.author = tc.author
			in := e.issueContext(context.Background(), b, pr, nil)
			if in.Issue == nil || in.Issue.Trusted != tc.trusted || in.Issue.Status != "In progress" {
				t.Fatalf("issue = %+v", in)
			}
		})
	}
}

func linkedFiles(urls ...string) []github.PullRequestFile {
	lines := make([]string, len(urls))
	for i, url := range urls {
		lines[i] = "+" + url
	}
	return []github.PullRequestFile{{Path: "decision.md", Additions: len(lines), Patch: strings.Join(lines, "\n")}}
}

// TestQuotedDecisionText: the service matches text the PR quotes against
// each trusted, unclipped decision, and keeps only what the decision
// contains. An untrusted decision gets no match, so it cannot confirm text.
func TestQuotedDecisionText(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", EditedAt: []byte("null"),
		Body: "Joe, 2026-11-17: lets keep one key for now.\nAdmin merge and   tune the review process with feedback."}
	api.comments[2] = &itsaplan.FeedItem{ID: 2, IssueID: 95, Kind: "comment", ActorUserID: "admin", EditedAt: []byte("null"),
		Body: "Admin merge and tune the review process with feedback."}
	pr.Body = "Joe decided:\n\n> Admin merge and tune the review\n> process with feedback.\n\n" +
		"He did not say \"skip the review for every PR from now on\", and \"short\" is too short."
	files := linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-59#comment-1", "https://factory-plan.jomcgi.dev/factory/issue/HOME-59#comment-2")
	files[0].Patch += "\n+Joe: “lets keep one key for now.”"
	in := e.issueContext(context.Background(), b, pr, files)
	if len(in.Decisions) != 2 {
		t.Fatalf("decisions = %+v", in.Decisions)
	}
	want := []string{"lets keep one key for now.", "Admin merge and tune the review process with feedback."}
	if got := in.Decisions[0].Quoted; !slices.Equal(got, want) {
		t.Errorf("trusted quotes = %q, want %q", got, want)
	}
	if got := in.Decisions[1].Quoted; len(got) != 0 {
		t.Errorf("an untrusted decision confirmed %q", got)
	}
}

func TestDecisionLinks(t *testing.T) {
	e, b, api, pr := issueSetup()
	for id, author := range map[int64]string{1: "human", 2: "sessions", 3: "bot", 4: "admin"} {
		api.comments[id] = &itsaplan.FeedItem{ID: id, IssueID: 95, Kind: "comment", ActorUserID: author, Body: "Decision from " + author, EditedAt: []byte("null")}
	}
	var urls []string
	for id := 1; id <= 5; id++ {
		urls = append(urls, fmt.Sprintf("https://factory-plan.jomcgi.dev/factory/issue/HOME-59#comment-%d", id))
	}
	urls = append(urls, "https://factory-plan.jomcgi.dev/factory/issue/HOME-57")
	in := e.issueContext(context.Background(), b, pr, linkedFiles(urls...))
	if api.issueReads != 1 || api.commentReads != 5 || len(in.Decisions) != 6 || len(in.Findings) != 0 {
		t.Fatalf("evidence = %+v; calls %+v", in, api)
	}
	for i, d := range in.Decisions {
		if d.Trusted != (i == 0 || i == 1) || d.Exists != (i != 4) {
			t.Errorf("decision %d = %+v", i, d)
		}
	}
	if in.Decisions[1].Author != "joesessions" {
		t.Fatalf("decision author = %q", in.Decisions[1].Author)
	}
}

func TestIssueEvidenceBounds(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.issue.Description = strings.Repeat("x", maxIssueText+1)
	var urls []string
	for id := 1; id <= 30; id++ {
		api.comments[int64(id)] = &itsaplan.FeedItem{ID: int64(id), IssueID: 95, Kind: "comment", ActorUserID: "human", Body: strings.Repeat("c", maxDecisionText+1)}
		urls = append(urls, fmt.Sprintf("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-%d", id))
	}
	in := e.issueContext(context.Background(), b, pr, linkedFiles(urls...))
	if api.issueReads != 1 || api.commentReads != maxDecisionLinks || len(in.Decisions) != maxDecisionLinks || len(in.Issue.Description) != maxIssueText {
		t.Fatal("evidence exceeded bounds")
	}
	for _, d := range in.Decisions {
		if len(d.Text) != maxDecisionText || !d.Clipped {
			t.Fatal("comment was not bounded")
		}
	}
	if len(in.Findings) != 2 {
		t.Fatalf("missing bound findings: %+v", in.Findings)
	}
	refs, incomplete := decisionRefs(b, linkedFiles(urls[0], urls[0]))
	if len(refs) != 1 || incomplete {
		t.Fatal("duplicate links counted twice")
	}
	_, incomplete = decisionRefs(b, []github.PullRequestFile{{Path: "a.md", Additions: 2, Patch: "+one"}})
	if !incomplete {
		t.Fatal("truncated patch accepted")
	}
	for _, suffix := range []string{"#comment-0", "#comment-1evil", "/another", "?redirect=other"} {
		refs, incomplete = decisionRefs(b, linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58"+suffix))
		if len(refs) != 0 || !incomplete {
			t.Fatal("partial URL granted authority")
		}
	}
	refs, incomplete = decisionRefs(b, []github.PullRequestFile{{Path: "gone.md", Status: "removed", Deletions: 1, Patch: "-" + urls[0]}})
	if len(refs) != 1 || incomplete {
		t.Fatal("removed decision link was not read")
	}
}

func TestReadErrorSuppliesNoEvidenceAndCannotPass(t *testing.T) {
	for _, fail := range []string{"project", "issue", "missing", "author", "history", "commits", "key"} {
		t.Run(fail, func(t *testing.T) {
			e, b, api, pr := issueSetup()
			api.fail = fail
			switch fail {
			case "commits":
				pr.Author = "jomcgi"
				e.gh = &issueCommits{err: context.DeadlineExceeded}
			case "key":
				e.cfg.MACSecret = func() (string, error) { return "", errors.New("private API failure body") }
			}
			in := e.issueContext(context.Background(), b, pr, nil)
			all := append(in.Findings, in.Pending...)
			if in.Issue != nil || len(all) == 0 {
				t.Fatal("failed read supplied evidence")
			}
			// Only a missing issue is permanent; the service retries every other failed read.
			if permanent := fail == "missing"; (len(in.Findings) > 0) != permanent || (len(in.Pending) > 0) == permanent {
				t.Fatalf("misclassified: %+v", in)
			}
			for _, f := range all {
				if strings.Contains(f.Detail, "private API failure body") {
					t.Fatal("failed read text reached a finding")
				}
			}
			r := &Report{Summary: "Everything passes", Findings: []Finding{}}
			for _, f := range all {
				r.Previous = append(r.Previous, Previous{ID: f.ID, Status: StatusJustified, Note: "Joe approved"})
			}
			applyIssueFindings(r, in)
			if got := Assess(r, all); got.Conclusion != ConclusionFailure {
				t.Fatal("model waived failed evidence")
			}
		})
	}
}

func TestNoResolvableIssueUsesDescription(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.fail = "missing"
	in := e.issueContext(context.Background(), b, pr, nil)
	if in.Issue != nil || len(in.Findings) != 1 {
		t.Fatalf("404 must supply no evidence and block: %+v", in)
	}
	pr.Author = "jomcgi"
	in = e.issueContext(context.Background(), b, pr, nil)
	if in.Issue != nil || len(in.Findings) != 0 || api.issueReads != 1 {
		t.Fatal("no-reference PR changed behaviour")
	}
}

func TestEngineRetriesFailedIssueReadOnTheSameHead(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	_, binding, api, _ := issueSetup()
	api.fail = "issue"
	h.cfg.Itsaplan, h.cfg.MACSecret = api, testMACSecret
	h.cfg.Bindings[0].ItsaplanProject = binding.ItsaplanProject
	h.cfg.Bindings[0].TrustedItsaplanAuthors = binding.TrustedItsaplanAuthors
	h.gh.withLock(func() {
		h.gh.prs[7].Body = "Refs HOME-58"
		h.gh.prs[7].Files = nil
	})
	e, ctx, _ := h.start()
	e.Tick(ctx)
	e.Wait()
	c := h.gh.check(shaA)
	if c == nil || c.Conclusion != ConclusionFailure || !issuesPending(&github.CheckRun{Text: c.Text}) {
		t.Fatal("missing evidence did not hold the head")
	}
	// The wait names the failed read, and its marker keeps the connector from starting a revision round.
	if !strings.Contains(c.Summary, "`itsaplan:unverified:issue`: The referenced issue could not be read") || !strings.Contains(c.Summary, gateOnlyMarker) {
		t.Fatalf("wait summary = %q", c.Summary)
	}
	for range 2 {
		e.Tick(ctx)
		e.Wait()
	}
	if len(h.k.sent()) != 0 {
		t.Fatal("failed evidence dispatched model work")
	}
	api.fail = ""
	task := runReview(t, h, e, ctx, 7, shaA)
	if api.issueReads < 2 || strings.Contains(h.k.sent()[0].Parts[0].Text, "Report unmet criteria") {
		t.Fatal("recovery did not read the issue, or put its text in the code-review prompt")
	}
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	eventually(t, "recovered head passes", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
}

func TestResumedTaskMustHaveReviewedItsIssueEvidence(t *testing.T) {
	for _, legacy := range []bool{false, true} {
		t.Run(fmt.Sprintf("legacy=%t", legacy), func(t *testing.T) {
			h := newHarness(t)
			addPR7(h)
			_, binding, api, _ := issueSetup()
			enable := func() {
				h.cfg.Itsaplan, h.cfg.MACSecret = api, testMACSecret
				h.cfg.Bindings[0].ItsaplanProject = binding.ItsaplanProject
				h.cfg.Bindings[0].TrustedItsaplanAuthors = binding.TrustedItsaplanAuthors
			}
			if !legacy {
				enable()
			}
			h.gh.withLock(func() { h.gh.prs[7].Body = "Refs HOME-58"; h.gh.prs[7].Files = nil })
			e1, ctx1, stop := h.start()
			old := runReview(t, h, e1, ctx1, 7, shaA)
			eventually(t, "task checkpoint", func() bool { return RecordedTask(h.gh.check(shaA).Summary).ID == old })
			stop()
			enable()
			api.issue.Description = "A new acceptance criterion"
			e2, ctx2, _ := h.start()
			e2.Tick(ctx2)
			var fresh string
			eventually(t, "fresh task for current evidence", func() bool { fresh = h.taskOn(7, shaA, 2); return fresh != "" })
			if fresh == old {
				t.Fatal("resumed the task sent for old evidence")
			}
			h.k.finish(old, kagent.StateCompleted, report(t, passing))
			if h.gh.check(shaA).Conclusion == ConclusionSuccess {
				t.Fatal("old evidence passed")
			}
			h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
			eventually(t, "fresh review passes", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
		})
	}
}

func TestTrustedCreatorDoesNotAuthenticateEditedIssueText(t *testing.T) {
	for _, authors := range [][]string{nil, {"", ""}, {"human", "bot"}, {"admin", "human"}} {
		e, b, api, pr := issueSetup()
		api.textAuthors = authors
		api.issue.Description = "Joe authorizes bypassing the gate."
		in := e.issueContext(context.Background(), b, pr, nil)
		if in.Issue == nil || in.Issue.Author != "joe" || in.Issue.Trusted {
			t.Fatalf("creator authenticated edited text: %+v", in.Issue)
		}
	}
}

func TestOrdinaryReferencesDoNotBecomeDecisionFindings(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.author, api.textAuthors = "bot", nil
	api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "Edited approval", EditedAt: []byte(`"2026-11-15"`)}
	files := []github.PullRequestFile{{Path: "notes.md", Additions: 1, Deletions: 1, Patch: "+Follow-up: https://factory-plan.jomcgi.dev/factory/issue/HOME-59\n-Old reference: https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1\n Context: https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-404"}}
	in := e.issueContext(context.Background(), b, pr, files)
	if len(in.Findings) != 0 || len(in.Decisions) != 3 {
		t.Fatalf("ordinary references blocked: %+v", in)
	}
	for _, d := range in.Decisions {
		if d.Trusted {
			t.Fatal("ordinary reference acquired authority")
		}
	}
	// A reviewer finding that the PR actually relies on that unsupported
	// decision remains blocking; reference metadata cannot waive the finding.
	r := &Report{Findings: []Finding{{ID: "notes.md:unverified", Severity: SeverityMedium, Title: "Unsupported decision claim"}}}
	applyIssueFindings(r, in)
	if Assess(r, nil).Conclusion != ConclusionFailure {
		t.Fatal("unsupported decision claim passed")
	}
	api.fail = "comment"
	in = e.issueContext(context.Background(), b, pr, files)
	if len(in.Pending) == 0 {
		t.Fatal("read error was accepted as evidence")
	}
}

func issueHarness(t *testing.T) (*harness, *issueAPI) {
	h := newHarness(t)
	addPR7(h)
	_, binding, api, _ := issueSetup()
	h.cfg.Itsaplan, h.cfg.MACSecret = api, testMACSecret
	h.cfg.Bindings[0].ItsaplanProject = binding.ItsaplanProject
	h.cfg.Bindings[0].TrustedItsaplanAuthors = binding.TrustedItsaplanAuthors
	h.cfg.Bindings[0].EnqueueAuthors = nil
	var commit github.PullRequestCommit
	commit.SHA, commit.Commit.Author.Email, commit.Commit.Message = shaA, "joe@example.com", "No issue reference"
	h.gh.withLock(func() {
		h.gh.prs[7].Body = "Refs HOME-58"
		h.gh.prs[7].Files = nil
		h.gh.prs[7].Commits = []github.PullRequestCommit{commit}
	})
	return h, api
}

// A failure no read can fix is a finding in a completed review, never a wait.
func TestPermanentEvidenceFailureIsReportedAsAFinding(t *testing.T) {
	h, api := issueHarness(t)
	api.fail = "missing"
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	c := h.gh.check(shaA)
	st, _ := LoadState(c.Text)
	if c.Conclusion != ConclusionFailure || st.IssuesPending || !strings.Contains(c.Summary, "Correct the PR's issue reference") {
		t.Fatalf("permanent failure was not reported as a finding: %q", c.Summary)
	}
	for range 2 {
		e.Tick(ctx)
		e.Wait()
	}
	if len(h.k.sent()) != 1 {
		t.Fatal("an unchanged permanent failure started another review")
	}
}

func TestEvidenceChangesBeforeCompletionRequireFreshReview(t *testing.T) {
	for _, failure := range []bool{false, true} {
		t.Run(fmt.Sprintf("readFailure=%t", failure), func(t *testing.T) {
			h, api := issueHarness(t)
			e, ctx, _ := h.start()
			old := runReview(t, h, e, ctx, 7, shaA)
			if failure {
				api.fail = "issue"
			} else {
				api.issue.Description = "New criterion"
			}
			h.k.finish(old, kagent.StateCompleted, report(t, passing))
			e.Wait()
			c := h.gh.check(shaA)
			if c.Conclusion != ConclusionFailure || !issuesPending(&github.CheckRun{Text: c.Text}) {
				t.Fatal("obsolete evidence passed")
			}
			api.fail = ""
			fresh := runReview(t, h, e, ctx, 7, shaA)
			if fresh == old {
				t.Fatal("the task for old evidence was reused")
			}
			h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
			e.Wait()
			if h.gh.check(shaA).Conclusion != ConclusionSuccess {
				t.Fatal("fresh evidence did not recover")
			}
		})
	}
}

func TestCompletedEvidenceIsRevalidatedAcrossRestartAndMergeQueue(t *testing.T) {
	for _, change := range []string{"goal", "status", "author", "decision", "read failure", "PR reference"} {
		t.Run(change, func(t *testing.T) {
			h, api := issueHarness(t)
			api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "Decision", EditedAt: []byte("null")}
			h.gh.withLock(func() {
				h.gh.prs[7].Files = linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
			})
			e, ctx, stop := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			if h.gh.check(shaA).Conclusion != ConclusionSuccess {
				t.Fatal("initial review failed")
			}
			h.gh.withLock(func() { h.gh.queue = []github.MergeQueueEntry{{Number: 7, PRHeadSHA: shaA, HeadCommit: "mg-issues"}} })
			e.Tick(ctx)
			if h.gh.check("mg-issues").Conclusion != ConclusionSuccess {
				t.Fatal("initial merge group failed")
			}
			stop()
			switch change {
			case "goal":
				api.issue.Description = "Changed goal"
			case "status":
				api.project.Columns[0].Name = "Done"
			case "author":
				api.textAuthors = []string{"human", "bot"}
			case "decision":
				api.comments[1].Body = "Changed decision"
				api.comments[1].EditedAt = []byte(`"2026-11-15"`)
			case "read failure":
				api.fail = "issue"
			case "PR reference":
				h.gh.withLock(func() { h.gh.prs[7].Body = "No reference" })
			}
			e, ctx, _ = h.start()
			e.Tick(ctx)
			if h.gh.check(shaA).Conclusion == ConclusionSuccess || h.gh.check("mg-issues").Conclusion != ConclusionFailure {
				t.Fatal("obsolete evidence retained a head or group pass")
			}
			if change == "read failure" {
				api.fail = ""
			}
			e.Tick(ctx)
			var fresh string
			eventually(t, "replacement review", func() bool { fresh = h.taskOn(7, shaA, 1); return fresh != "" && fresh != task })
			h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
			e.Wait()
			if h.gh.check(shaA).Conclusion != ConclusionSuccess {
				t.Fatal("recovery needs another push")
			}
		})
	}
}

func TestEvidenceRefreshPreservesFindingsAndHumanHolds(t *testing.T) {
	h, api := issueHarness(t)
	h.gh.label(7, "human")
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, map[string]any{"summary": "Blocked", "findings": []any{
		map[string]any{"id": "x.go:correctness", "severity": "high", "title": "Unresolved defect", "detail": "Must remain blocked"},
	}}))
	e.Wait()
	api.issue.Description = "Changed criteria"
	fresh := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
	e.Wait()
	c := h.gh.check(shaA)
	st, ok := LoadState(c.Text)
	if !ok || c.Conclusion != ConclusionFailure || len(st.Blocking) == 0 || st.Gate == nil || !st.Gate.Needed() {
		t.Fatal("evidence refresh cleared findings or human hold")
	}
	if st.IssueDigest == "" {
		t.Fatal("completed gate lost its evidence binding")
	}
}

func TestIssueRevalidationWriteErrorsCannotAuthorizeMergeGroups(t *testing.T) {
	for _, refuseCreate := range []bool{false, true} {
		t.Run(fmt.Sprintf("refuseCreate=%t", refuseCreate), func(t *testing.T) {
			h, api := issueHarness(t)
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			h.gh.withLock(func() { h.gh.queue = []github.MergeQueueEntry{{Number: 7, PRHeadSHA: shaA, HeadCommit: "mg-issues"}} })
			e.Tick(ctx)
			if h.gh.check("mg-issues").Conclusion != ConclusionSuccess {
				t.Fatal("group did not initially pass")
			}
			api.fail = "project"
			path := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
			h.gh.failWith(path, 502)
			if refuseCreate {
				h.gh.withLock(func() { h.gh.failCheckSHA = shaA })
			}
			e.Tick(ctx)
			if h.gh.check("mg-issues").Conclusion != ConclusionFailure || len(h.gh.enqueues()) != 0 {
				t.Fatal("write error propagated obsolete evidence")
			}
			if !refuseCreate && h.gh.check(shaA).Conclusion != ConclusionFailure {
				t.Fatal("new failing check did not revoke pass")
			}
		})
	}
}

func TestNoReferenceNeedsNoIssueReadsOrRepeatedReview(t *testing.T) {
	h, api := issueHarness(t)
	h.gh.withLock(func() { h.gh.prs[7].Body = "Update the pin" })
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	for range 2 {
		e.Tick(ctx)
	}
	if api.issueReads != 0 || len(h.k.sent()) != 1 || h.gh.check(shaA).Conclusion != ConclusionSuccess {
		t.Fatal("no-reference behavior changed")
	}
}

func TestIssueRecoveryDoesNotResetExhaustedModelAttempts(t *testing.T) {
	h, api := issueHarness(t)
	h.gh.label(7, "human")
	e, ctx, _ := h.start()
	runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "model attempts exhausted", func() bool {
		h.k.finishOpen(kagent.StateFailed, "No report")
		return h.gh.check(shaA).Status == "completed"
	})
	e.Wait()
	api.fail = "project"
	e.Tick(ctx)
	api.fail = ""
	api.issue.Description = "Changed goal"
	h.gh.review(7, "jomcgi", "APPROVED", shaA, "Approved")
	for range 2 {
		e.Tick(ctx)
		e.Wait()
	}
	c := h.gh.check(shaA)
	if c.Conclusion != ConclusionFailure || c.Title != "No review report" || len(h.k.sent()) != MaxAttempts {
		t.Fatal("issue recovery or approval reset model exhaustion or passed without a report")
	}
}

func TestIssueEvidenceHoldAndRecoveryUpdateTheExistingComment(t *testing.T) {
	for _, refuseUpdate := range []bool{false, true} {
		t.Run(fmt.Sprintf("refuseUpdate=%t", refuseUpdate), func(t *testing.T) {
			h, api := issueHarness(t)
			withComments(h)
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			initial := onlyComment(t, h, 7)
			if !commentHas(h, 7, verdictHeading[VerdictApproved]) {
				t.Fatal("completed issue review was not mirrored")
			}
			api.fail = "issue"
			if refuseUpdate {
				path := fmt.Sprintf("PATCH /repos/%s/check-runs/%d", testRepo, h.gh.check(shaA).ID)
				h.gh.failWith(path, 502)
			}
			e.Tick(ctx)
			e.Wait()
			if !commentHas(h, 7, verdictHeading[VerdictHold], "Waiting for current issue evidence", "no push is required") {
				t.Fatal("comment retained approval after the evidence hold")
			}
			if onlyComment(t, h, 7).ID != initial.ID {
				t.Fatal("evidence hold posted another comment")
			}
			api.fail = ""
			fresh := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
			e.Wait()
			if h.gh.check(shaA).Conclusion != ConclusionSuccess || !commentHas(h, 7, verdictHeading[VerdictApproved]) {
				t.Fatal("fresh review did not update the check and comment")
			}
			if onlyComment(t, h, 7).ID != initial.ID {
				t.Fatal("recovery posted another comment")
			}
		})
	}
}

// The check is public and issues are private (Joe's decision on #8116): only
// the factory App's and humanApprovers' PRs get issue or decision reads.
func TestIssueEvidenceOnlyForTrustedAuthors(t *testing.T) {
	for author, reads := range map[string]bool{"outsider": false, "jomcgi": true, "jomcgi-factory[bot]": true} {
		t.Run(author, func(t *testing.T) {
			h, api := issueHarness(t)
			api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "Decision", EditedAt: []byte("null")}
			h.gh.withLock(func() {
				h.gh.prs[7].Author = author
				h.gh.prs[7].Files = linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
			})
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			e.Tick(ctx)
			if (api.issueReads > 0) != reads || (api.commentReads > 0) != reads {
				t.Fatalf("%d issue and %d comment reads", api.issueReads, api.commentReads)
			}
			if h.gh.check(shaA).Conclusion != ConclusionSuccess {
				t.Fatal("the review did not continue without evidence")
			}
		})
	}
}

// The code review gets no issue text (Joe's decision on #8116): it reviews
// the diff, title and description, as with no issue.
func TestCodeReviewPromptHasNoIssueText(t *testing.T) {
	for _, author := range []string{"jomcgi", "jomcgi-factory[bot]"} {
		t.Run(author, func(t *testing.T) {
			h, api := issueHarness(t)
			api.issue.Title, api.issue.Description = "PRIVATE-TITLE", "PRIVATE-GOAL"
			api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "PRIVATE-DECISION", EditedAt: []byte("null")}
			h.gh.withLock(func() {
				h.gh.prs[7].Author, h.gh.prs[7].HeadRef = author, "factory/HOME-58"
				h.gh.prs[7].Files = linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
			})
			e, ctx, _ := h.start()
			runReview(t, h, e, ctx, 7, shaA)
			if api.issueReads == 0 || api.commentReads == 0 {
				t.Fatal("trusted author's evidence was not read")
			}
			p := h.k.sent()[0].Parts[0].Text
			if strings.Contains(p, "PRIVATE") || !strings.Contains(p, "the PR description stands in") {
				t.Fatal("issue text reached the code-review prompt")
			}
			// The link's provenance verdict reaches it, without the text.
			if !strings.Contains(p, "- `https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1`: verified provenance, but the PR quotes none of its text, so what it says is unverified.\n") {
				t.Fatalf("the code-review prompt lacks the link's verdict:\n%s", p)
			}
		})
	}
}

// TestLinkedVerdicts: the code review gets fixed text for each linked work
// item, and never its author or text.
func TestLinkedVerdicts(t *testing.T) {
	base := "https://factory-plan.jomcgi.dev/factory/issue/HOME-1"
	p := Prompt(PromptInput{Repo: testRepo, Number: 7, Base: "main", SHA: shaA, Linked: []DecisionEvidence{
		{URL: base + "#comment-1", Author: "joesessions", Text: "PRIVATE-1 Keep one key for now.", Exists: true, Trusted: true, Reason: "verified authorship", Quoted: []string{"Keep one key for now."}},
		{URL: base + "#comment-5", Author: "joe", Text: "PRIVATE-5", Exists: true, Trusted: true, Reason: "verified authorship"},
		{URL: base + "#comment-2", Author: "factoryadmin", Text: "PRIVATE-2", Exists: true, Reason: "untrusted author"},
		{URL: base + "#comment-3", Author: "joe", Text: "PRIVATE-3", Exists: true, Trusted: true, Clipped: true, Reason: "text exceeds bound"},
		{URL: base + "#comment-4", Reason: "unreadable or missing"},
	}})
	for _, want := range []string{
		"- `" + base + "#comment-1`: verified provenance; it contains the quoted text below.\n  - It contains: \"Keep one key for now.\"\n",
		"- `" + base + "#comment-5`: verified provenance, but the PR quotes none of its text, so what it says is unverified.\n",
		"- `" + base + "#comment-2`: unverified: a trusted account did not write its current text.\n",
		"- `" + base + "#comment-3`: verified provenance, but its text is longer than the service reads, so what it says is unverified.\n",
		"- `" + base + "#comment-4`: missing or unreadable; it is no evidence.\n",
	} {
		if !strings.Contains(p, want) {
			t.Errorf("prompt lacks %q", want)
		}
	}
	if strings.Contains(p, "PRIVATE") || strings.Contains(p, "factoryadmin") || strings.Contains(p, "joesessions") || strings.Contains(p, "`joe`") {
		t.Fatalf("a linked item's text or author reached the prompt:\n%s", p)
	}
	if strings.Contains(Prompt(PromptInput{Repo: testRepo, Number: 7, Base: "main", SHA: shaA}), "## Linked work items") {
		t.Fatal("a PR with no links got the section")
	}
}

// The check publishes the evidence digest, and the evidence holds private
// issue and decision text, so the digest is an HMAC under a derived key.
func TestEvidenceDigestIsNotPlainSHA256(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "Private decision", EditedAt: []byte("null")}
	in := e.issueContext(context.Background(), b, pr, linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1"))
	key, err := e.evidenceKey()
	secret, _ := testMACSecret()
	if in.Issue == nil || len(in.Decisions) != 1 || err != nil || string(key) == secret {
		t.Fatalf("evidence = %+v, key error %v", in, err)
	}
	data, _ := json.Marshal(in)
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	if plain := sha256.Sum256(data); issueDigest(in) != hex.EncodeToString(mac.Sum(nil)) || issueDigest(in) == hex.EncodeToString(plain[:]) {
		t.Fatalf("digest %q is not the evidence MAC", issueDigest(in))
	}
}

func TestEvidenceDigestDiffersByKey(t *testing.T) {
	read := func(secret string) *IssueContext {
		e, b, _, pr := issueSetup()
		e.cfg.MACSecret = func() (string, error) { return secret, nil }
		return e.issueContext(context.Background(), b, pr, nil)
	}
	a, again, other := read("first-itsaplan-key-0123456789"), read("first-itsaplan-key-0123456789"), read("other-itsaplan-key-0123456789")
	if issueDigest(a) == "" || issueDigest(a) != issueDigest(again) || issueDigest(a) == issueDigest(other) {
		t.Fatalf("digests %q, %q, %q", issueDigest(a), issueDigest(again), issueDigest(other))
	}
	// A secret too short to key the MAC is a failed read: no evidence, no digest.
	if in := read("short"); in.Issue != nil || issueDigest(in) != "" || len(in.Pending) != 1 || in.Pending[0].ID != "itsaplan:unverified:key" {
		t.Fatalf("short secret = %+v", in)
	}
}

// A restart reads the same mounted secret, so the MAC and the pass stand.
func TestEvidenceDigestSurvivesRestartWithTheSameKey(t *testing.T) {
	h, _ := issueHarness(t)
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	before, _ := LoadState(h.gh.check(shaA).Text)
	sends := len(h.k.sent()) // The code review and, with a goal, its check.
	stop()
	e, ctx, _ = h.start()
	for range 2 {
		e.Tick(ctx)
		e.Wait()
	}
	after, _ := LoadState(h.gh.check(shaA).Text)
	if before.IssueDigest == "" || after.IssueDigest != before.IssueDigest || h.gh.check(shaA).Conclusion != ConclusionSuccess ||
		len(h.k.sent()) != sends || sends-len(goalTexts(h)) != 1 {
		t.Fatal("a restart with the same key changed the evidence binding")
	}
}

// unkeyedHashes are plain SHA-2 hashes of each value, as hex and base64.
func unkeyedHashes(values []string) []string {
	var out []string
	for _, v := range values {
		a, b, c := sha256.Sum224([]byte(v)), sha256.Sum256([]byte(v)), sha512.Sum512([]byte(v))
		for _, sum := range [][]byte{a[:], b[:], c[:]} {
			out = append(out, hex.EncodeToString(sum), base64.StdEncoding.EncodeToString(sum), base64.RawURLEncoding.EncodeToString(sum))
		}
	}
	return out
}

// Every published field: check titles, summaries (the task marker), texts
// and their decoded state, and the PR comment. None carries private issue or
// decision text, or an unkeyed hash of it or of the whole evidence.
func TestPublishedOutputCarriesNoUnkeyedEvidenceHash(t *testing.T) {
	h, api := issueHarness(t)
	withComments(h)
	api.issue.Title, api.issue.Description = "PRIVATE-TITLE", "PRIVATE-GOAL"
	api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "PRIVATE-DECISION", EditedAt: []byte("null")}
	files := linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
	h.gh.withLock(func() { h.gh.prs[7].Author, h.gh.prs[7].Files = "jomcgi", files })
	pr := github.PullRequest{Number: 7, Author: "jomcgi", Body: "Refs HOME-58", HeadSHA: shaA}
	var published, private, digests []string
	snapshot := func() {
		h.gh.withLock(func() {
			for _, c := range h.gh.checks {
				st, _ := LoadState(c.Text)
				data, _ := json.Marshal(st)
				published = append(published, c.Title, c.Summary, c.Text, c.ExternalID, string(data))
			}
		})
		for _, c := range h.gh.ownComments(7) {
			published = append(published, c.Body)
		}
		in := h.engine().issueContext(context.Background(), &h.cfg.Bindings[0], pr, files)
		if in.Issue == nil || len(in.Decisions) != 1 {
			t.Fatalf("evidence = %+v", in)
		}
		data, _ := json.Marshal(in)
		private = append(private, string(data), in.Issue.Title, in.Issue.Description, in.Decisions[0].Text)
		digests = append(digests, issueDigest(in))
	}
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	eventually(t, "task marker", func() bool { return RecordedTask(h.gh.check(shaA).Summary).Issues != "" })
	snapshot()
	marker := RecordedTask(h.gh.check(shaA).Summary).Issues
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	snapshot()
	st, _ := LoadState(h.gh.check(shaA).Text)
	api.issue.Description = "PRIVATE-GOAL-CHANGED" // The hold and the fresh review publish too.
	e.Tick(ctx)
	var fresh string
	eventually(t, "fresh task marker", func() bool { fresh = RecordedTask(h.gh.check(shaA).Summary).ID; return fresh != "" && fresh != task })
	snapshot()
	h.k.finish(fresh, kagent.StateCompleted, report(t, passing))
	e.Wait()
	snapshot()
	// The fields hold the MAC of the evidence this test hashes.
	if marker != digests[0] || st.IssueDigest != digests[1] || digests[0] != digests[1] || digests[2] == digests[0] {
		t.Fatalf("published %q and %q, evidence MACs %q", marker, st.IssueDigest, digests)
	}
	forbidden := append(private, unkeyedHashes(private)...)
	for _, field := range published {
		for _, bad := range forbidden {
			if strings.Contains(field, bad) || strings.Contains(strings.ToLower(field), bad) {
				t.Fatalf("a published field carries private text or its unkeyed hash %q", bad)
			}
		}
	}
}
