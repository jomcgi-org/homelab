package capabilities

import (
	"context"
	"fmt"
	"os/exec"
)

// Git provides the workspace operations a shim hook needs: clone a repository
// into a guest directory and push local commits back to the origin.
type Git interface {
	// Clone replicates the repository at mirror into dest, then checks out ref.
	// ref may be a branch name, tag, or commit SHA.
	Clone(ctx context.Context, mirror, ref, dest string) error

	// Push sends the commits on branch in the repository at dest to its
	// configured origin remote.
	Push(ctx context.Context, dest, branch string) error
}

// runnerFunc is the function signature used to execute a subprocess. The
// default implementation wraps exec.CommandContext; tests inject a fake to
// record invocations without spawning real processes. This is a test seam
// only: leave the field nil in production.
type runnerFunc func(ctx context.Context, name string, args ...string) ([]byte, error)

// defaultRunner executes name with args under ctx and returns combined stdout
// and stderr output.
func defaultRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).CombinedOutput()
}

// ExecGit is a Git implementation that shells out to the git binary using
// os/exec, with no third-party dependencies.
//
// Bin is the path to the git executable. When Bin is empty the string "git"
// is used, resolved via PATH.
//
// The runner field is a test seam: leave it nil for production use (the
// defaultRunner is applied automatically); inject a fakeRunner in tests to
// assert on invocation arguments without spawning real processes.
type ExecGit struct {
	// Bin is the git binary path. Defaults to "git" when empty.
	Bin string

	// runner is the subprocess executor; nil means defaultRunner.
	runner runnerFunc
}

func (g *ExecGit) bin() string {
	if g.Bin != "" {
		return g.Bin
	}
	return "git"
}

func (g *ExecGit) run(ctx context.Context, args ...string) error {
	r := g.runner
	if r == nil {
		r = defaultRunner
	}
	out, err := r(ctx, g.bin(), args...)
	if err != nil {
		return fmt.Errorf("git %v: %w\noutput: %s", args, err, out)
	}
	return nil
}

// Clone clones the repository at mirror into dest, then checks out ref.
// Using clone-then-checkout means any ref type (branch, tag, full SHA) works.
func (g *ExecGit) Clone(ctx context.Context, mirror, ref, dest string) error {
	if err := g.run(ctx, "clone", mirror, dest); err != nil {
		return fmt.Errorf("git Clone (clone): %w", err)
	}
	if err := g.run(ctx, "-C", dest, "checkout", ref); err != nil {
		return fmt.Errorf("git Clone (checkout %s): %w", ref, err)
	}
	return nil
}

// Push pushes the commits on branch in the repository at dest to origin.
func (g *ExecGit) Push(ctx context.Context, dest, branch string) error {
	if err := g.run(ctx, "-C", dest, "push", "origin", branch); err != nil {
		return fmt.Errorf("git Push: %w", err)
	}
	return nil
}

// Compile-time check: ExecGit satisfies Git.
var _ Git = (*ExecGit)(nil)
