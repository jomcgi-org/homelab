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
	defaultSemgrepBin = "/opt/semgrep-venv/bin/semgrep"
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

	if err := setupOfflineEnv(logger); err != nil {
		return err
	}

	if err := os.MkdirAll(workspaceDir, 0o755); err != nil {
		return err
	}

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

	// Announce readiness to the host controller over the control channel. Best-effort:
	// off-cluster there is no controller, and the scan server still serves locally.
	announceReady(logger)

	ln, err := scanserver.Listen(vsockproto.ScanPort)
	if err != nil {
		return err
	}
	logger.Info("scan server listening", "port", vsockproto.ScanPort)
	srv := &scanserver.Server{Scanner: driver, Logger: logger}
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
