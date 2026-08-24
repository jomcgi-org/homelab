// Package refresh keeps a mirrors root warm: one bare clone per configured
// GitHub repository under <root>/<owner>/<repo>.git, re-fetched on an interval
// so session hydration reads a fresh-to-one-tick-old copy of upstream instead
// of GitHub itself (#4473).
//
// Layout and refspec mirror the retired central mirror's contract: heads and
// tags only, never refs/pull/*, pruned so deleted upstream branches disappear
// here too. The optional token exists for private repos; it travels per
// invocation as an extraheader argument and is never written to disk or logs.
package refresh

import (
	"context"
	"encoding/base64"
	"fmt"
	"log/slog"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultGitBin = "git"
	githubURLBase = "https://github.com/"
)

// Config is one Refresher's inputs. Repos are "owner/name" strings.
type Config struct {
	Root     string
	GitBin   string
	Repos    []string
	Interval time.Duration
	// Token is an optional GitHub read credential for private repos.
	Token string
	// UpstreamBase defaults to GitHub; tests point it at a local fixture path.
	UpstreamBase string
}

// Refresher runs the fetch loop over a Config.
type Refresher struct {
	cfg    Config
	logger *slog.Logger
}

// New builds a Refresher. Interval must be positive; GitBin defaults to "git"
// and UpstreamBase to GitHub.
func New(cfg Config, logger *slog.Logger) *Refresher {
	if cfg.GitBin == "" {
		cfg.GitBin = defaultGitBin
	}
	if cfg.UpstreamBase == "" {
		cfg.UpstreamBase = githubURLBase
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Refresher{cfg: cfg, logger: logger}
}

// Run blocks until ctx is done, syncing every repo immediately and then once
// per interval. A failing repo logs and retries next tick; it never stops the
// loop, because a partial mirror serving its good repos beats no mirror.
func (r *Refresher) Run(ctx context.Context) {
	r.syncAll(ctx)
	ticker := time.NewTicker(r.cfg.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.syncAll(ctx)
		}
	}
}

func (r *Refresher) syncAll(ctx context.Context) {
	for _, repo := range r.cfg.Repos {
		if err := r.syncRepo(ctx, repo); err != nil {
			r.logger.Error("mirror: refresh failed", "repo", repo, "err", err)
		}
	}
	r.logger.Info("mirror: refresh pass complete", "repos", len(r.cfg.Repos))
}

// RepoDir is where repo's bare clone lives (or will live).
func (r *Refresher) RepoDir(repo string) string {
	return filepath.Join(r.cfg.Root, repo+".git")
}

func (r *Refresher) syncRepo(ctx context.Context, repo string) error {
	if !validRepoName(repo) {
		return fmt.Errorf("invalid repo %q", repo)
	}
	dir := r.RepoDir(repo)
	exists, err := r.isBareRepo(dir)
	if err != nil {
		return err
	}
	if !exists {
		if err := r.initialClone(ctx, repo, dir); err != nil {
			return err
		}
	}
	return r.fetch(ctx, repo, dir)
}

// initialClone creates the bare skeleton and pulls heads+tags the first time.
// It deliberately avoids `git clone --mirror`: that would also drag GitHub's
// refs/pull/* into every node's disk for content nobody clones.
func (r *Refresher) initialClone(ctx context.Context, repo, dir string) error {
	if out, err := r.git(ctx,
		"-c", "gc.auto=0",
		"init",
		"--bare",
		"--initial-branch=main",
		dir,
	); err != nil {
		return fmt.Errorf("init %s: %w: %s", dir, err, out)
	}
	if out, err := r.git(ctx,
		"-C", dir,
		"remote", "add", "origin", r.cfg.UpstreamBase+repo+".git",
	); err != nil {
		return fmt.Errorf("remote add %s: %w: %s", repo, err, out)
	}
	// Partial-clone support advertised by this repo forever, matching the
	// retired mirror ("Enable partial/shallow clones"). Belt-and-braces with
	// the -c the server passes on every request.
	if _, err := r.git(ctx,
		"-C", dir,
		"config", "uploadpack.allowFilter", "true",
	); err != nil {
		return fmt.Errorf("config %s: %w", repo, err)
	}
	if err := r.fetch(ctx, repo, dir); err != nil {
		return err
	}
	r.pointHeadAtDefault(ctx, dir)
	return nil
}

func (r *Refresher) fetch(ctx context.Context, repo, dir string) error {
	out, err := r.git(ctx,
		"-C", dir,
		"-c", "gc.auto=0",
		"fetch",
		"--prune",
		"--force",
		"origin",
		"+refs/heads/*:refs/heads/*",
		"+refs/tags/*:refs/tags/*",
	)
	if err != nil {
		return fmt.Errorf("fetch %s: %w: %s", repo, err, out)
	}
	return nil
}

// pointHeadAtDefault aims the bare clone's HEAD at upstream's default branch,
// best-effort: a clone without --branch resolves HEAD, so a dangling HEAD
// would fail exactly those hydrations. Failures log and move on because a
// stale HEAD self-corrects on the next pass and hydration mostly pins --branch.
func (r *Refresher) pointHeadAtDefault(ctx context.Context, dir string) {
	out, err := r.git(ctx, "-C", dir, "ls-remote", "--symref", "origin", "HEAD")
	if err != nil {
		r.logger.Warn("mirror: could not read upstream HEAD", "dir", dir, "err", err)
		return
	}
	for _, line := range strings.Split(string(out), "\n") {
		ref, found := strings.CutPrefix(line, "ref: ")
		if !found {
			continue
		}
		target, ok := strings.CutPrefix(ref, "refs/heads/")
		fields := strings.Fields(target)
		if ok && len(fields) > 0 {
			if _, err := r.git(ctx, "-C", dir, "symbolic-ref", "HEAD", "refs/heads/"+fields[0]); err != nil {
				r.logger.Warn("mirror: could not set HEAD", "dir", dir, "err", err)
			}
		}
		return
	}
}

// authArgs renders the per-invocation credential for private repos. It rides
// argv only: nothing lands in the repo config, on disk, or in any log line.
// With no token the clone stays anonymous (public repos need nothing).
func authArgs(token string) []string {
	if token == "" {
		return nil
	}
	basic := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + token))
	return []string{
		"-c", "http.https://github.com/.extraheader=Authorization: Basic " + basic,
	}
}

func validRepoName(repo string) bool {
	owner, name, ok := strings.Cut(repo, "/")
	if !ok || owner == "" || name == "" {
		return false
	}
	return !strings.ContainsAny(owner, "./ \t\n") && !strings.ContainsAny(name, "./ \t\n")
}

func (r *Refresher) isBareRepo(dir string) (bool, error) {
	out, err := r.git(context.Background(), "-C", dir, "rev-parse", "--is-bare-repository")
	if err != nil {
		// rev-parse fails outside any repository, which here means
		// not-initialized-yet rather than an operational failure.
		return false, nil
	}
	return strings.TrimSpace(string(out)) == "true", nil
}

// git runs one git invocation with this refresher's binary plus the shared
// auth extraheader when a token is configured.
func (r *Refresher) git(ctx context.Context, args ...string) ([]byte, error) {
	argv := append([]string{r.cfg.GitBin}, authArgs(r.cfg.Token)...)
	argv = append(argv, args...)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	return cmd.CombinedOutput()
}
