// Command semgrep-guest-init is the PID 1 of the semgrep scanner microVM. It
// keeps a warm offline-Pro semgrep scan-server (osemgrep-pro, parsers warmed and
// rules compiled once) and serves scan requests over the fc-invoke shim
// protocol: an HTTP server over AF_VSOCK on vsockproto.GuestHTTPPort. Readiness
// is signalled via GET /shim/ready (200 once the scan-server has printed
// {"ready":true}, 503 before) so fc-invoke can poll and the host snapshots the
// VM only after warmup.
//
// The engine runs fully offline. A placeholder-token settings file (written by
// setupEnv) satisfies the Pro entitlement check without any network call: all
// three fields are load-bearing, and the placeholder api_token unlocks the Pro
// engine because only the presence of the setting is checked, never validated.
// No SEMGREP_APP_TOKEN is set.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"

	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scandriver"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scanserver"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	defaultOsemgrepPro = "/opt/semgrep/osemgrep-pro"
	defaultRulesDir    = "/etc/semgrep/rules"
	guestHome          = "/tmp/sghome"
	settingsFileName   = "settings.yml"
)

// offlineSettings is the semgrep settings file the scan-server reads. All three
// fields are load-bearing: without them the settings fail to decode and the Pro
// engine gate trips. The placeholder api_token unlocks the Pro engine offline
// (presence, not validity, is checked) and never leaves the guest.
const offlineSettings = `anonymous_user_id: 00000000-0000-0000-0000-000000000000
has_shown_metrics_notification: true
api_token: placeholder-not-real
`

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

	// Mount a tmpfs over /tmp so all mutable guest state (the scan-server HOME and
	// its settings file) lives in RAM, not the rootfs. This lets the rootfs stay
	// read-only and be shared by every microVM restored from one warm-base
	// snapshot. Must precede the guestHome MkdirAll so it lands on the tmpfs.
	mountTmpfsTmp(logger)

	// A raw Firecracker boot gives PID 1 no environment, so PATH is empty and
	// every execvp fails with ENOENT. semgrep-core shells out to `uname` to build
	// its TLS authenticator at startup, so an unset PATH crashes it even though
	// uname is installed. Set a standard PATH plus a UTF-8 locale.
	for k, v := range map[string]string{
		"PATH":   "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"LANG":   "C.UTF-8",
		"LC_ALL": "C.UTF-8",
	} {
		_ = os.Setenv(k, v)
	}

	settingsFile, err := setupEnv(logger)
	if err != nil {
		return err
	}

	bin := envOr("OSEMGREP_PRO_BIN", defaultOsemgrepPro)
	rulesDir := envOr("SEMGREP_SCAN_RULES", defaultRulesDir)
	logger.Info("starting offline-Pro scan-server", "bin", bin, "rules", rulesDir)

	// Start blocks until the scan-server prints {"ready":true} (parsers warmed,
	// rules compiled) — the point at which the host may snapshot the VM. The
	// engine warms itself, so there is no separate primer pass. The child's
	// lifetime is bound to ctx: a SIGTERM cancels it and Close reaps it.
	driver, err := scandriver.Start(ctx, bin, rulesDir, settingsFile)
	if err != nil {
		return err
	}
	defer driver.Close()
	logger.Info("scan-server warm; ready to serve")

	// Flip readiness only after the scan-server is warm: /shim/ready returns 503
	// until this Store, 200 after, so fc-invoke will not route scan requests
	// before warmup and the host snapshots a warm VM. Starts false (atomic.Bool
	// zero value).
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

// setupEnv writes the offline settings file into the guest HOME (on the tmpfs)
// and sets the environment the scan-server reads. It returns the settings file
// path. No SEMGREP_APP_TOKEN is set: the placeholder api_token in the settings
// file is what unlocks the Pro engine offline.
func setupEnv(logger *slog.Logger) (string, error) {
	if err := os.MkdirAll(guestHome, 0o755); err != nil {
		return "", err
	}
	settingsFile := guestHome + "/" + settingsFileName
	if err := os.WriteFile(settingsFile, []byte(offlineSettings), 0o600); err != nil {
		return "", err
	}
	for k, v := range map[string]string{
		"HOME":                         guestHome,
		"SEMGREP_SETTINGS_FILE":        settingsFile,
		"SEMGREP_SEND_METRICS":         "off",
		"SEMGREP_ENABLE_VERSION_CHECK": "0",
	} {
		if err := os.Setenv(k, v); err != nil {
			return "", err
		}
	}
	logger.Info("offline scan-server env set", "home", guestHome, "settings", settingsFile)
	return settingsFile, nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
