package capabilities

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// Git provides the workspace operations a shim hook needs: clone a repository
// into a guest directory and push commits back to a remote with an explicit
// refspec (WS3 scratch-ref recording).
type Git interface {
	// Clone replicates the repository at mirror into dest using a shallow
	// partial clone (--single-branch --depth=1 --filter=blob:none), then
	// checks out ref. ref may be a branch name, tag, or commit SHA.
	Clone(ctx context.Context, mirror, ref, dest string) error

	// PushRefspec pushes refspec from the repository at dest to remoteURL.
	// For the scratch-ref recording path the refspec is "HEAD:refs/agents/<session>".
	PushRefspec(ctx context.Context, dest, remoteURL, refspec string) error
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

// run executes a git sub-command and returns only an error. Output bytes are
// included in the error message when the command fails.
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

// runOutput executes a git sub-command and returns the output bytes. It is
// used by callers that need to inspect stdout/stderr content (e.g.
// git status --porcelain for change detection).
func (g *ExecGit) runOutput(ctx context.Context, args ...string) ([]byte, error) {
	r := g.runner
	if r == nil {
		r = defaultRunner
	}
	out, err := r(ctx, g.bin(), args...)
	if err != nil {
		return nil, fmt.Errorf("git %v: %w\noutput: %s", args, err, out)
	}
	return out, nil
}

// Clone clones the repository at mirror into dest using a single-branch partial
// clone (--single-branch --filter=blob:none), then checks out ref. It keeps the
// FULL commit history of the branch (so `git log`, blame, and "recent commits"
// tasks work) while deferring file contents: --filter=blob:none fetches commits
// and trees up front and lazily pulls a blob only when a file is opened, which
// keeps the clone fast against the in-cluster mirror without the --depth=1
// shallow cut that left the workspace with only the tip commit (ADR 026). Both
// branch names and full SHAs work.
func (g *ExecGit) Clone(ctx context.Context, mirror, ref, dest string) error {
	if err := g.run(ctx, "clone", "--single-branch", "--filter=blob:none", mirror, dest); err != nil {
		return fmt.Errorf("git Clone (clone): %w", err)
	}
	if err := g.run(ctx, "-C", dest, "checkout", ref); err != nil {
		return fmt.Errorf("git Clone (checkout %s): %w", ref, err)
	}
	return nil
}

// PushRefspec pushes refspec from the repository at dest to remoteURL.
// For the WS3 scratch-ref recording path the refspec is
// "HEAD:refs/agents/<session>" and remoteURL is the in-cluster mirror address.
func (g *ExecGit) PushRefspec(ctx context.Context, dest, remoteURL, refspec string) error {
	if err := g.run(ctx, "-C", dest, "push", remoteURL, refspec); err != nil {
		return fmt.Errorf("git PushRefspec: %w", err)
	}
	return nil
}

// RecordScratch commits any workspace changes and pushes them to
// refs/agents/<session> on mirrorURL (WS3). It returns the pushed ref name on
// success, or an empty string when the workspace is clean (nothing to commit).
// A non-nil error means the commit or push failed; the caller treats this as
// best-effort and continues without failing the run.
//
// Shallow-push validity: the workspace was cloned shallow from the same mirror,
// so the mirror already has the base commit. If git refuses the push due to a
// shallow boundary, RecordScratch unshallows the clone and retries once.
func (g *ExecGit) RecordScratch(ctx context.Context, workspace, mirrorURL, session string) (string, error) {
	ref := "refs/agents/" + sanitizeRefComponent(session)

	// Set a bot git identity so the commit is attributed correctly.
	if err := g.run(ctx, "-C", workspace, "config", "user.name", "goosecracker"); err != nil {
		return "", fmt.Errorf("git RecordScratch (config name): %w", err)
	}
	if err := g.run(ctx, "-C", workspace, "config", "user.email", "agent@jomcgi.dev"); err != nil {
		return "", fmt.Errorf("git RecordScratch (config email): %w", err)
	}

	// Stage all workspace changes (new files, modifications, deletions).
	if err := g.run(ctx, "-C", workspace, "add", "-A"); err != nil {
		return "", fmt.Errorf("git RecordScratch (add): %w", err)
	}

	// Check for staged changes via git status --porcelain. Empty output means
	// the workspace is clean; nothing to commit or push.
	out, err := g.runOutput(ctx, "-C", workspace, "status", "--porcelain")
	if err != nil {
		return "", fmt.Errorf("git RecordScratch (status): %w", err)
	}
	if len(strings.TrimSpace(string(out))) == 0 {
		return "", nil // no changes; nothing to push
	}

	// Commit with a message that identifies the session for later retrieval.
	msg := "agent " + session + ": workspace changes"
	if err := g.run(ctx, "-C", workspace, "commit", "-m", msg); err != nil {
		return "", fmt.Errorf("git RecordScratch (commit): %w", err)
	}

	// Push to the mirror. The workspace was cloned shallow from the same mirror
	// (WS2), so the mirror already has the base commit and the push should
	// succeed. If git refuses due to the shallow boundary, unshallow the clone
	// and retry once.
	refspec := "HEAD:" + ref
	if err := g.PushRefspec(ctx, workspace, mirrorURL, refspec); err != nil {
		// Attempt to unshallow the clone and retry the push.
		if fetchErr := g.run(ctx, "-C", workspace, "fetch", "--unshallow"); fetchErr != nil {
			return "", fmt.Errorf(
				"git RecordScratch (push failed, unshallow also failed): push=%w unshallow=%v",
				err, fetchErr,
			)
		}
		if err := g.PushRefspec(ctx, workspace, mirrorURL, refspec); err != nil {
			return "", fmt.Errorf("git RecordScratch (push after unshallow): %w", err)
		}
	}
	return ref, nil
}

// sanitizeRefComponent replaces characters that are invalid in a git ref
// component with a hyphen, and trims leading/trailing hyphens and dots. A
// Discord snowflake (all digits) and a UUID (hex + hyphens) both pass through
// unchanged; only adversarial or unusual session ids are rewritten.
func sanitizeRefComponent(s string) string {
	var b strings.Builder
	for _, c := range s {
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9',
			c == '-', c == '_', c == '.':
			b.WriteRune(c)
		default:
			b.WriteByte('-')
		}
	}
	result := strings.Trim(b.String(), "-.")
	// Remove double-dot sequences, which are invalid in git refs.
	for strings.Contains(result, "..") {
		result = strings.ReplaceAll(result, "..", ".")
	}
	if result == "" {
		return "session"
	}
	return result
}

// Compile-time check: ExecGit satisfies Git.
var _ Git = (*ExecGit)(nil)
