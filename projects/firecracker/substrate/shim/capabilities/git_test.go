package capabilities

import (
	"context"
	"testing"
)

// fakeGit is a test double for the Git interface that records every call made
// to it. Use it in handler tests to assert on the Clone/Push arguments without
// touching the filesystem or a real git process.
type fakeGit struct {
	cloneCalls []cloneCall
	pushCalls  []pushCall
}

type cloneCall struct {
	Mirror string
	Ref    string
	Dest   string
}

type pushCall struct {
	Dest   string
	Branch string
}

func (f *fakeGit) Clone(_ context.Context, mirror, ref, dest string) error {
	f.cloneCalls = append(f.cloneCalls, cloneCall{Mirror: mirror, Ref: ref, Dest: dest})
	return nil
}

func (f *fakeGit) Push(_ context.Context, dest, branch string) error {
	f.pushCalls = append(f.pushCalls, pushCall{Dest: dest, Branch: branch})
	return nil
}

// Compile-time check: fakeGit satisfies Git.
var _ Git = (*fakeGit)(nil)

// fakeRunnerSpy records every (name, args) invocation of ExecGit's runner seam
// and returns a configurable output/error pair. Inject it via ExecGit.runner.
type fakeRunnerSpy struct {
	calls  [][]string // each element is [name, arg0, arg1, ...]
	output []byte
	err    error
}

func (f *fakeRunnerSpy) run(ctx context.Context, name string, args ...string) ([]byte, error) {
	call := make([]string, 0, 1+len(args))
	call = append(call, name)
	call = append(call, args...)
	f.calls = append(f.calls, call)
	return f.output, f.err
}

// TestExecGitCloneInvokesGit verifies that Clone issues "git clone <mirror>
// <dest>" followed by "git -C <dest> checkout <ref>", without spawning a real
// process.
func TestExecGitCloneInvokesGit(t *testing.T) {
	spy := &fakeRunnerSpy{}
	g := &ExecGit{runner: spy.run}

	ctx := context.Background()
	if err := g.Clone(ctx, "https://github.com/example/repo", "main", "/tmp/dst"); err != nil {
		t.Fatalf("Clone: %v", err)
	}

	if len(spy.calls) != 2 {
		t.Fatalf("expected 2 git invocations, got %d: %v", len(spy.calls), spy.calls)
	}

	// First call: git clone <mirror> <dest>
	cloneArgs := spy.calls[0]
	if len(cloneArgs) < 4 || cloneArgs[1] != "clone" || cloneArgs[2] != "https://github.com/example/repo" || cloneArgs[3] != "/tmp/dst" {
		t.Errorf("first call = %v, want [git clone <mirror> <dest>]", cloneArgs)
	}

	// Second call: git -C <dest> checkout <ref>
	checkoutArgs := spy.calls[1]
	if len(checkoutArgs) < 5 || checkoutArgs[1] != "-C" || checkoutArgs[2] != "/tmp/dst" || checkoutArgs[3] != "checkout" || checkoutArgs[4] != "main" {
		t.Errorf("second call = %v, want [git -C <dest> checkout <ref>]", checkoutArgs)
	}
}

// TestExecGitPushInvokesGit verifies that Push issues
// "git -C <dest> push origin <branch>".
func TestExecGitPushInvokesGit(t *testing.T) {
	spy := &fakeRunnerSpy{}
	g := &ExecGit{runner: spy.run}

	ctx := context.Background()
	if err := g.Push(ctx, "/tmp/dst", "my-branch"); err != nil {
		t.Fatalf("Push: %v", err)
	}

	if len(spy.calls) != 1 {
		t.Fatalf("expected 1 git invocation, got %d: %v", len(spy.calls), spy.calls)
	}
	// Expect: git -C /tmp/dst push origin my-branch
	pushArgs := spy.calls[0]
	if len(pushArgs) < 6 || pushArgs[1] != "-C" || pushArgs[2] != "/tmp/dst" || pushArgs[3] != "push" || pushArgs[4] != "origin" || pushArgs[5] != "my-branch" {
		t.Errorf("push call = %v, want [git -C <dest> push origin <branch>]", pushArgs)
	}
}

// TestFakeGitRecordsCalls verifies the fakeGit double faithfully records every
// Clone and Push invocation for use in handler tests.
func TestFakeGitRecordsCalls(t *testing.T) {
	ctx := context.Background()
	g := &fakeGit{}

	if err := g.Clone(ctx, "mirror-url", "sha1abc", "/dst"); err != nil {
		t.Fatalf("Clone: %v", err)
	}
	if err := g.Push(ctx, "/dst", "feat/x"); err != nil {
		t.Fatalf("Push: %v", err)
	}

	if len(g.cloneCalls) != 1 {
		t.Fatalf("cloneCalls = %d, want 1", len(g.cloneCalls))
	}
	want := cloneCall{Mirror: "mirror-url", Ref: "sha1abc", Dest: "/dst"}
	if g.cloneCalls[0] != want {
		t.Errorf("cloneCall = %+v, want %+v", g.cloneCalls[0], want)
	}

	if len(g.pushCalls) != 1 {
		t.Fatalf("pushCalls = %d, want 1", len(g.pushCalls))
	}
	wantPush := pushCall{Dest: "/dst", Branch: "feat/x"}
	if g.pushCalls[0] != wantPush {
		t.Errorf("pushCall = %+v, want %+v", g.pushCalls[0], wantPush)
	}
}
