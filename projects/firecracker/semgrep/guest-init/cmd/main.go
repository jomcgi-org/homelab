// Command semgrep-guest-init is the PID 1 of the semgrep scanner microVM. It
// keeps a `semgrep lsp` process warm (rules loaded and compiled once) and
// serves scan requests over the fc-invoke shim protocol: an HTTP server over
// AF_VSOCK on vsockproto.GuestHTTPPort. Readiness is signalled via
// GET /shim/ready (returns 200 once the LSP is warm, 503 before that) so
// fc-invoke can poll instead of relying on the legacy KindHello control-port
// announcement.
//
// The semgrep environment is set OFFLINE before the process starts. This is
// load-bearing: a non-empty SEMGREP_APP_TOKEN (even the literal string
// "offline") is treated as a real login token and makes startup hang 10-60s
// on a Semgrep cloud fetch. An empty token plus an isolated throwaway
// HOME/settings keeps it fully local.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/lspdriver"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scanserver"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	defaultSemgrepBin = "/usr/bin/semgrep"
	defaultRulesDir   = "/etc/semgrep/rules"
	defaultCoreBin    = "/opt/semgrep/semgrep-core-proprietary"
	guestHome         = "/tmp/sghome"
	workspaceDir      = "/tmp/sgwork"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("semgrep-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Raw FC boot leaves loopback DOWN; bring it up before anything binds 127.0.0.1.
	bringUpLoopback(logger)

	// Mount a tmpfs over /tmp so all mutable guest state (the semgrep workspace,
	// its HOME, the git repo) lives in RAM, not the rootfs. This lets the rootfs
	// stay read-only and be shared by every microVM restored from one warm-base
	// snapshot. Must precede the workspace/HOME MkdirAll calls so they land on
	// the tmpfs.
	mountTmpfsTmp(logger)

	// A raw Firecracker boot gives PID 1 no environment, so PATH is empty and
	// every execvp fails with ENOENT. semgrep-core shells out to `uname` to build
	// its TLS authenticator at startup, so an unset PATH crashes it even though
	// uname is installed. Set a standard PATH plus a UTF-8 locale for pysemgrep.
	for k, v := range map[string]string{
		"PATH":   "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"LANG":   "C.UTF-8",
		"LC_ALL": "C.UTF-8",
	} {
		_ = os.Setenv(k, v)
	}

	if err := setupOfflineEnv(logger); err != nil {
		return err
	}

	if err := os.MkdirAll(workspaceDir, 0o755); err != nil {
		return err
	}

	// Make the workspace its own git repo. semgrep lsp computes scan targets
	// through git: when the workspace is a git project, a scan file that is
	// `git add`-ed (see lspdriver.writeWorkspaceFile) becomes a tracked target
	// and is scanned; without a git project the LSP finds no targets and returns
	// no findings. The repo is empty and local (no remote, no commits), purely
	// to root semgrep's target walk.
	initWorkspaceGit(logger)

	bin := envOr("SEMGREP_BIN", defaultSemgrepBin)
	rulesDir := envOr("SEMGREP_RULES_DIR", defaultRulesDir)
	jobs := jobsFromEnv()
	logger.Info("starting semgrep lsp", "bin", bin, "rules", rulesDir, "jobs", jobs)

	driver, err := lspdriver.Spawn(ctx, bin, workspaceDir)
	if err != nil {
		return err
	}
	defer driver.Close()

	// Initialize + warm-up. The warm-up (rules compile) is the slow part, so
	// give it a generous deadline distinct from per-scan timeouts.
	readyCtx, cancel := context.WithTimeout(ctx, durationEnv("SEMGREP_READY_TIMEOUT", 90*time.Second))
	defer cancel()
	if err := driver.Initialize(readyCtx, rulesDir, jobs); err != nil {
		return err
	}
	if err := driver.WaitReady(readyCtx); err != nil {
		return err
	}
	logger.Info("semgrep lsp warm; rules compiled")

	// Warm the LSP with one realistic scan per supported language BEFORE
	// announcing readiness, so the base snapshot (taken once the host sees the
	// shim's /shim/ready=200) captures the per-process warmup the first real
	// scan otherwise pays. Restored guests then scan their first file already
	// warmed (~3-4x faster first scan). Best-effort: a prime failure is logged,
	// not fatal (the guest still serves, just cold-first).
	warmupPrime(ctx, driver, logger)

	// Flip the readiness flag only after Initialize + WaitReady + warmupPrime.
	// This is the critical correctness point: /shim/ready returns 503 until this
	// Store, then 200 after, so fc-invoke will not route scan requests before the
	// LSP is warm. The flag starts false (atomic.Bool zero value).
	var ready atomic.Bool
	ready.Store(true)

	ln, err := scanserver.ListenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		return err
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	h := handler.New(driver)
	srv := shim.NewServer(h, shim.WithReady(ready.Load))

	// Serve in a goroutine so ctx cancellation (SIGTERM) can close the server
	// gracefully rather than blocking indefinitely on Accept.
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()

	select {
	case <-ctx.Done():
		_ = srv.Close()
		<-serveErr
		return nil
	case err := <-serveErr:
		return err
	}
}

// setupOfflineEnv sets the OFFLINE semgrep environment. These values are
// load-bearing (see package doc): the empty token and isolated HOME/settings
// keep semgrep from reaching the Semgrep cloud at startup.
func setupOfflineEnv(logger *slog.Logger) error {
	if err := os.MkdirAll(guestHome, 0o755); err != nil {
		return err
	}
	// Force-set (not ensure): an empty SEMGREP_APP_TOKEN is the correct value
	// and must win even over an inherited token.
	for k, v := range map[string]string{
		"SEMGREP_APP_TOKEN":            "",
		"HOME":                         guestHome,
		"SEMGREP_SETTINGS_FILE":        guestHome + "/settings.yml",
		"SEMGREP_SEND_METRICS":         "off",
		"SEMGREP_ENABLE_VERSION_CHECK": "0",
	} {
		if err := os.Setenv(k, v); err != nil {
			return err
		}
	}
	// The baked Pro engine core; let an explicit override win, otherwise default.
	if os.Getenv("SEMGREP_CORE_BIN") == "" {
		if err := os.Setenv("SEMGREP_CORE_BIN", defaultCoreBin); err != nil {
			return err
		}
	}
	logger.Info("offline semgrep env set", "home", guestHome)
	return nil
}

// warmupPrime scans the per-language primer set (primers.go) once so the
// one-time per-process warm-up is captured in the base snapshot. Bounded by
// SEMGREP_PRIME_TIMEOUT; best-effort, a failure is logged not fatal.
func warmupPrime(ctx context.Context, driver *lspdriver.Driver, logger *slog.Logger) {
	start := time.Now()
	pctx, cancel := context.WithTimeout(ctx, durationEnv("SEMGREP_PRIME_TIMEOUT", 45*time.Second))
	defer cancel()
	findings, err := driver.Scan(pctx, primerFiles)
	if err != nil {
		logger.Warn("warmup prime failed", "err", err, "took", time.Since(start))
		return
	}
	logger.Info("warmup prime done", "languages", len(primerFiles), "primer_findings", len(findings), "took", time.Since(start))
}

// initWorkspaceGit makes workspaceDir an empty local git repo so semgrep lsp's
// git-based target discovery roots at the workspace. Best-effort: a failure
// only degrades scanning, so each step logs and continues rather than aborting
// boot.
func initWorkspaceGit(logger *slog.Logger) {
	for _, args := range [][]string{
		{"init"},
		{"config", "user.email", "scan@local"},
		{"config", "user.name", "scan"},
	} {
		cmd := exec.Command("git", append([]string{"-C", workspaceDir}, args...)...)
		if out, err := cmd.CombinedOutput(); err != nil {
			logger.Warn("workspace git setup step failed", "args", args, "err", err, "out", string(out))
		}
	}
}

func jobsFromEnv() int {
	if v := os.Getenv("SEMGREP_JOBS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	if n := runtime.NumCPU(); n > 0 {
		return n
	}
	return 1
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
