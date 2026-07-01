package capabilities

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// spyResponse holds the (output, error) pair a fakeRunnerSpy returns for one
// git call. It powers per-call response sequences in RecordScratch tests.
type spyResponse struct {
	output []byte
	err    error
}

// fakeGit is a test double for the Git interface that records every call made
// to it. Use it in handler tests to assert on the Clone/PushRefspec arguments
// without touching the filesystem or a real git process.
type fakeGit struct {
	cloneCalls       []cloneCall
	pushRefspecCalls []pushRefspecCall
}

type cloneCall struct {
	Mirror string
	Ref    string
	Dest   string
}

type pushRefspecCall struct {
	Dest      string
	RemoteURL string
	Refspec   string
}

func (f *fakeGit) Clone(_ context.Context, mirror, ref, dest string) error {
	f.cloneCalls = append(f.cloneCalls, cloneCall{Mirror: mirror, Ref: ref, Dest: dest})
	return nil
}

func (f *fakeGit) PushRefspec(_ context.Context, dest, remoteURL, refspec string) error {
	f.pushRefspecCalls = append(f.pushRefspecCalls, pushRefspecCall{Dest: dest, RemoteURL: remoteURL, Refspec: refspec})
	return nil
}

// Compile-time check: fakeGit satisfies Git.
var _ Git = (*fakeGit)(nil)

// fakeRunnerSpy records every (name, args) invocation of ExecGit's runner seam
// and returns either per-call responses (responses slice) or a single default
// output/error pair. Inject it via ExecGit.runner.
type fakeRunnerSpy struct {
	calls     [][]string    // each element is [name, arg0, arg1, ...]
	responses []spyResponse // per-call; last entry is reused once exhausted
	output    []byte        // default output when responses is nil or empty
	err       error         // default error when responses is nil or empty
}

func (f *fakeRunnerSpy) run(ctx context.Context, name string, args ...string) ([]byte, error) {
	call := make([]string, 0, 1+len(args))
	call = append(call, name)
	call = append(call, args...)
	idx := len(f.calls)
	f.calls = append(f.calls, call)
	if len(f.responses) > 0 {
		ri := idx
		if ri >= len(f.responses) {
			ri = len(f.responses) - 1
		}
		return f.responses[ri].output, f.responses[ri].err
	}
	return f.output, f.err
}

// TestExecGitClonePassesPartialFlags verifies that Clone issues a single-branch
// partial clone (--single-branch --filter=blob:none, NOT --depth=1 so full commit
// history is present) followed by "git -C <dest> checkout <ref>", without
// spawning a real process.
func TestExecGitClonePassesPartialFlags(t *testing.T) {
	spy := &fakeRunnerSpy{}
	g := &ExecGit{runner: spy.run}

	ctx := context.Background()
	if err := g.Clone(ctx, "git://mirror:9418/homelab", "main", "/tmp/dst"); err != nil {
		t.Fatalf("Clone: %v", err)
	}

	if len(spy.calls) != 2 {
		t.Fatalf("expected 2 git invocations, got %d: %v", len(spy.calls), spy.calls)
	}

	// First call: git clone --single-branch --filter=blob:none <mirror> <dest>
	cloneArgs := spy.calls[0]
	// Verify key positions: command is "clone", partial flags are present, mirror and dest are last.
	if len(cloneArgs) < 2 || cloneArgs[1] != "clone" {
		t.Fatalf("first call must be git clone, got %v", cloneArgs)
	}
	joined := strings.Join(cloneArgs, " ")
	for _, want := range []string{"--single-branch", "--filter=blob:none", "git://mirror:9418/homelab", "/tmp/dst"} {
		if !strings.Contains(joined, want) {
			t.Errorf("clone call %q missing %q", joined, want)
		}
	}
	// --depth=1 must NOT be present (it would drop commit history).
	if strings.Contains(joined, "--depth") {
		t.Errorf("clone call %q must not be shallow (--depth), it drops commit history", joined)
	}

	// Second call: git -C <dest> checkout <ref>
	checkoutArgs := spy.calls[1]
	if len(checkoutArgs) < 5 || checkoutArgs[1] != "-C" || checkoutArgs[2] != "/tmp/dst" || checkoutArgs[3] != "checkout" || checkoutArgs[4] != "main" {
		t.Errorf("second call = %v, want [git -C <dest> checkout <ref>]", checkoutArgs)
	}
}

// TestExecGitPushRefspecInvokesGit verifies that PushRefspec issues
// "git -C <dest> push <remoteURL> <refspec>".
func TestExecGitPushRefspecInvokesGit(t *testing.T) {
	spy := &fakeRunnerSpy{}
	g := &ExecGit{runner: spy.run}

	ctx := context.Background()
	if err := g.PushRefspec(ctx, "/tmp/dst", "git://mirror:9418/homelab", "HEAD:refs/agents/sess-1"); err != nil {
		t.Fatalf("PushRefspec: %v", err)
	}

	if len(spy.calls) != 1 {
		t.Fatalf("expected 1 git invocation, got %d: %v", len(spy.calls), spy.calls)
	}
	// Expect: git -C /tmp/dst push git://mirror:9418/homelab HEAD:refs/agents/sess-1
	pushArgs := spy.calls[0]
	if len(pushArgs) < 6 ||
		pushArgs[1] != "-C" ||
		pushArgs[2] != "/tmp/dst" ||
		pushArgs[3] != "push" ||
		pushArgs[4] != "git://mirror:9418/homelab" ||
		pushArgs[5] != "HEAD:refs/agents/sess-1" {
		t.Errorf("push call = %v, want [git -C <dest> push <remoteURL> <refspec>]", pushArgs)
	}
}

// TestExecGitRecordScratchSequencesGitCommands verifies the full command
// sequence for a workspace with staged changes: config, add, status (returns
// non-empty), commit, push.
func TestExecGitRecordScratchSequencesGitCommands(t *testing.T) {
	spy := &fakeRunnerSpy{
		responses: []spyResponse{
			{nil, nil},                    // git config user.name
			{nil, nil},                    // git config user.email
			{nil, nil},                    // git add -A
			{[]byte("M  file.go\n"), nil}, // git status --porcelain: has changes
			{nil, nil},                    // git commit
			{nil, nil},                    // git push
		},
	}
	g := &ExecGit{runner: spy.run}

	ref, err := g.RecordScratch(context.Background(), "/ws", "git://mirror:9418/repo", "sess-1")
	if err != nil {
		t.Fatalf("RecordScratch: %v", err)
	}
	if ref == "" {
		t.Error("expected non-empty ref when workspace has changes")
	}
	if ref != "refs/agents/sess-1" {
		t.Errorf("ref = %q, want refs/agents/sess-1", ref)
	}

	// Verify push was called with the right remote URL and refspec.
	var pushCall []string
	for _, c := range spy.calls {
		if len(c) > 3 && c[3] == "push" {
			pushCall = c
		}
	}
	if pushCall == nil {
		t.Fatal("no push call found in git invocations")
	}
	if len(pushCall) < 6 || pushCall[4] != "git://mirror:9418/repo" || pushCall[5] != "HEAD:refs/agents/sess-1" {
		t.Errorf("push call = %v, want remote=git://mirror:9418/repo refspec=HEAD:refs/agents/sess-1", pushCall)
	}
}

// TestExecGitRecordScratchNoChangesSkipsCommit verifies that RecordScratch
// returns ("", nil) and calls no commit or push when git status reports a
// clean workspace.
func TestExecGitRecordScratchNoChangesSkipsCommit(t *testing.T) {
	spy := &fakeRunnerSpy{
		responses: []spyResponse{
			{nil, nil},        // git config user.name
			{nil, nil},        // git config user.email
			{nil, nil},        // git add -A
			{[]byte(""), nil}, // git status --porcelain: empty = no changes
		},
	}
	g := &ExecGit{runner: spy.run}

	ref, err := g.RecordScratch(context.Background(), "/ws", "git://mirror:9418/repo", "sess-2")
	if err != nil {
		t.Fatalf("RecordScratch (no changes): %v", err)
	}
	if ref != "" {
		t.Errorf("expected empty ref when no changes, got %q", ref)
	}

	// No commit or push should have been issued.
	for _, c := range spy.calls {
		joined := strings.Join(c, " ")
		if strings.Contains(joined, "commit") || strings.Contains(joined, "push") {
			t.Errorf("unexpected git call when workspace is clean: %v", c)
		}
	}
}

// TestExecGitRecordScratchUnshallowFallback verifies that when the initial
// push fails, RecordScratch unshallows the clone and retries the push.
func TestExecGitRecordScratchUnshallowFallback(t *testing.T) {
	pushErr := errors.New("shallow update not allowed")
	spy := &fakeRunnerSpy{
		responses: []spyResponse{
			{nil, nil},                    // git config user.name
			{nil, nil},                    // git config user.email
			{nil, nil},                    // git add -A
			{[]byte("M  file.go\n"), nil}, // git status --porcelain
			{nil, nil},                    // git commit
			{nil, pushErr},                // git push: first attempt fails
			{nil, nil},                    // git fetch --unshallow
			{nil, nil},                    // git push: retry after unshallow
		},
	}
	g := &ExecGit{runner: spy.run}

	ref, err := g.RecordScratch(context.Background(), "/ws", "git://mirror:9418/repo", "sess-3")
	if err != nil {
		t.Fatalf("RecordScratch (unshallow fallback): %v", err)
	}
	if ref == "" {
		t.Error("expected non-empty ref after successful retry")
	}

	// Verify fetch --unshallow was called.
	var sawUnshallow bool
	for _, c := range spy.calls {
		if len(c) >= 4 && c[3] == "fetch" && strings.Join(c, " ") != "" {
			joined := strings.Join(c, " ")
			if strings.Contains(joined, "--unshallow") {
				sawUnshallow = true
			}
		}
	}
	if !sawUnshallow {
		t.Error("expected git fetch --unshallow to be called after push failure")
	}
}

// TestSanitizeRefComponent verifies the sanitize helper for common session id
// shapes (snowflake, UUID, adversarial input).
func TestSanitizeRefComponent(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"123456789012345678", "123456789012345678"}, // Discord snowflake (all digits)
		{"a1b2c3d4-e5f6-7890", "a1b2c3d4-e5f6-7890"}, // UUID-like
		{"s-abc123def456", "s-abc123def456"},         // generated session id
		{"feat/thing", "feat-thing"},                 // slash replaced
		{"has space", "has-space"},                   // space replaced
		{"", "session"},                              // empty fallback
		{"a..b", "a.b"},                              // double-dot collapsed
	}
	for _, c := range cases {
		got := sanitizeRefComponent(c.in)
		if got != c.want {
			t.Errorf("sanitizeRefComponent(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// TestFakeGitRecordsCalls verifies the fakeGit double faithfully records every
// Clone and PushRefspec invocation for use in handler tests.
func TestFakeGitRecordsCalls(t *testing.T) {
	ctx := context.Background()
	g := &fakeGit{}

	if err := g.Clone(ctx, "mirror-url", "sha1abc", "/dst"); err != nil {
		t.Fatalf("Clone: %v", err)
	}
	if err := g.PushRefspec(ctx, "/dst", "git://mirror:9418/repo", "HEAD:refs/agents/s1"); err != nil {
		t.Fatalf("PushRefspec: %v", err)
	}

	if len(g.cloneCalls) != 1 {
		t.Fatalf("cloneCalls = %d, want 1", len(g.cloneCalls))
	}
	wantClone := cloneCall{Mirror: "mirror-url", Ref: "sha1abc", Dest: "/dst"}
	if g.cloneCalls[0] != wantClone {
		t.Errorf("cloneCall = %+v, want %+v", g.cloneCalls[0], wantClone)
	}

	if len(g.pushRefspecCalls) != 1 {
		t.Fatalf("pushRefspecCalls = %d, want 1", len(g.pushRefspecCalls))
	}
	wantPush := pushRefspecCall{Dest: "/dst", RemoteURL: "git://mirror:9418/repo", Refspec: "HEAD:refs/agents/s1"}
	if g.pushRefspecCalls[0] != wantPush {
		t.Errorf("pushRefspecCall = %+v, want %+v", g.pushRefspecCalls[0], wantPush)
	}
}
