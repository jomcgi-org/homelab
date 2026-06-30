// Command semgrep-guest-init is the PID 1 of the semgrep scanner microVM. It keeps
// a `semgrep lsp` process warm (rules loaded and compiled once), announces
// readiness to the host controller over vsock, and then serves whole-file scan
// requests on vsockproto.ScanPort: each request is a batch of in-memory files, each
// response is the semgrep findings.
//
// The semgrep environment is set OFFLINE before the process starts. This is
// load-bearing: a non-empty SEMGREP_APP_TOKEN (even the literal string "offline")
// is treated as a real login token and makes startup hang 10-60s on a Semgrep cloud
// fetch. An empty token plus an isolated throwaway HOME/settings keeps it fully
// local.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-guest-init/internal/lspdriver"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-guest-init/internal/scanserver"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
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

	// A raw Firecracker boot gives PID 1 no environment, so PATH is empty and every
	// execvp fails with ENOENT. semgrep-core shells out to `uname` to build its TLS
	// authenticator at startup, so an unset PATH crashes it even though uname is
	// installed. Set a standard PATH plus a UTF-8 locale for pysemgrep.
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

	// Make the workspace its own git repo. semgrep lsp computes scan targets through
	// git: when the workspace is a git project, a scan file that is `git add`-ed (see
	// lspdriver.writeWorkspaceFile) becomes a tracked target and is scanned; without
	// a git project the LSP finds no targets and returns no findings. The repo is
	// empty and local (no remote, no commits), purely to root semgrep's target walk.
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

	// Initialize + warm-up. The warm-up (rules compile) is the slow part, so give it
	// a generous deadline distinct from per-scan timeouts.
	readyCtx, cancel := context.WithTimeout(ctx, durationEnv("SEMGREP_READY_TIMEOUT", 90*time.Second))
	defer cancel()
	if err := driver.Initialize(readyCtx, rulesDir, jobs); err != nil {
		return err
	}
	if err := driver.WaitReady(readyCtx); err != nil {
		return err
	}
	logger.Info("semgrep lsp warm; rules compiled")

	ln, err := scanserver.Listen(vsockproto.ScanPort)
	if err != nil {
		return err
	}
	logger.Info("scan server listening", "port", vsockproto.ScanPort)

	// Announce readiness only after the scan port is bound, so the host can never
	// dial the scan port before the guest is listening. Best-effort: off-cluster
	// there is no controller, and the scan server still serves locally.
	announceReady(logger)

	srv := &scanserver.Server{
		Scanner:     driver,
		Logger:      logger,
		ScanTimeout: durationEnv("SEMGREP_SCAN_TIMEOUT", 55*time.Second),
	}
	return srv.Serve(ctx, ln)
}

// setupOfflineEnv sets the OFFLINE semgrep environment. These values are
// load-bearing (see package doc): the empty token and isolated HOME/settings keep
// semgrep from reaching the Semgrep cloud at startup.
func setupOfflineEnv(logger *slog.Logger) error {
	if err := os.MkdirAll(guestHome, 0o755); err != nil {
		return err
	}
	// Force-set (not ensure): an empty SEMGREP_APP_TOKEN is the correct value and
	// must win even over an inherited token.
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

// announceReady dials the host ControlPort and sends a Hello so the controller
// knows the scanner is warm and accepting scans.
func announceReady(logger *slog.Logger) {
	rwc, err := dialVsock(vsockproto.HostCID, vsockproto.ControlPort)
	if err != nil {
		logger.Info("no controller on vsock; serving scans locally", "err", err)
		return
	}
	conn := vsockproto.NewConn(rwc)
	defer conn.Close()
	if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindHello, ThreadID: os.Getenv("FC_THREAD_ID")}); err != nil {
		logger.Warn("failed to announce readiness", "err", err)
		return
	}
	logger.Info("readiness announced to controller")
}

// initWorkspaceGit makes workspaceDir an empty local git repo so semgrep lsp's
// git-based target discovery roots at the workspace. Best-effort: a failure only
// degrades scanning, so each step logs and continues rather than aborting boot.
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
