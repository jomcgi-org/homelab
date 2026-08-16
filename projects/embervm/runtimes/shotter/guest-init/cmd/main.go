// Command ember-shotter-init is PID 1 of the shotter task-class microVM (ADR
// embervm/035). noded cuts the base snapshot when GET /shim/ready first returns
// 200, so this process preserves one load-bearing order:
//
//  1. Mount /proc and a deliberately sized tmpfs over /tmp.
//  2. Start the vsock HTTP server with readiness still false.
//  3. Load the egress destination policy; a failure here fails closed too.
//  4. Launch long-lived Chromium with console fds, then poll /json/version.
//  5. Run one real trial capture over CDP (navigate, screenshot, close), the
//     same T3 code path an invocation uses, so a browser that answers CDP
//     but cannot actually render is caught here, loudly, once.
//  6. Flip readiness so the snapshot captures the live, render-tested browser.
//
// A warm failure never flips readiness. BuildBase then fails on its outer boot
// timeout instead of silently snapshotting a cold, half-started, or
// non-rendering browser.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	chromiumBinary  = "/usr/bin/chromium"
	chromiumHome    = "/tmp/shotter-home"
	chromiumProfile = "/tmp/shotter-profile"
	cdpAddress      = "127.0.0.1"
	cdpPort         = 9222
	cdpPollInterval = 100 * time.Millisecond
	cdpProbeTimeout = 500 * time.Millisecond
	cdpWarmTimeout  = 60 * time.Second
	screenshotPath  = "/shim/screenshot"

	// trialRenderTimeout bounds the one real capture run during warm-up.
	// /json/version answering only proves the CDP protocol is alive, not
	// that Chromium can navigate and rasterize: a browser broken by a
	// missing font or a broken software-GL path answers /json/version fine
	// and would otherwise get baked into the base snapshot, failing every
	// real invocation forever instead of once, loudly, here.
	trialRenderTimeout = 15 * time.Second
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-shotter-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Raw Firecracker boot does not consume the OCI environment or mount the
	// filesystems normal userspace expects.
	mountProc(logger)
	mountTmpfsTmp(logger)
	setDefaultEnv(logger)
	bringUpLoopback(logger)
	setHostname(logger)
	proxyConfig, proxyConfigErr := LoadProxyConfig()
	if proxyConfigErr != nil {
		logger.Warn("ember-shotter-init: proxy configuration invalid; all destinations refused", "err", proxyConfigErr)
	}
	proxyErr, err := startProxyServer(ctx, proxyConfig, logger)
	if err != nil {
		return fmt.Errorf("start egress proxy: %w", err)
	}

	var ready atomic.Bool
	serveErr := startVsockServer(ctx, logger, ready.Load)

	if proxyConfigErr != nil {
		// A warm failure never flips readiness (see the package doc above).
		// An invalid or absent egress policy means every screenshot this
		// base ever serves would fail with its destination refused, so
		// there is nothing warming Chromium would buy: fail readiness now,
		// the same way a Chromium launch or CDP probe failure already does,
		// instead of letting BuildBase snapshot a browser that answers CDP
		// on about:blank (no network needed) but can reach nothing.
		logger.Error("ember-shotter-init: proxy configuration invalid; readiness remains false", "err", proxyConfigErr)
		return waitForShutdown(ctx, serveErr, proxyErr, nil, logger)
	}

	chromiumExit, err := launchChromium(ctx, logger)
	if err != nil {
		logger.Error("ember-shotter-init: Chromium warm launch failed; readiness remains false", "err", err)
		return waitForShutdown(ctx, serveErr, proxyErr, nil, logger)
	}

	if err := waitForCDP(ctx, chromiumExit, logger); err != nil {
		logger.Error("ember-shotter-init: CDP warm probe failed; readiness remains false", "err", err)
		// Chromium's Wait goroutine still reaps the process. Keep PID 1 and the
		// readiness server alive so BuildBase reports its outer readiness timeout.
		return waitForShutdown(ctx, serveErr, proxyErr, nil, logger)
	}

	logger.Info("ember-shotter-init: warm ordering checkpoint 2, CDP /json/version answered")
	logger.Info("ember-shotter-init: running a trial capture before readiness", "timeout", trialRenderTimeout.String())
	if err := trialRender(ctx, logger); err != nil {
		logger.Error("ember-shotter-init: trial render failed; readiness remains false", "err", err)
		return waitForShutdown(ctx, serveErr, proxyErr, chromiumExit, logger)
	}

	// This log is the ordering assertion used with the preceding checkpoint:
	// ready cannot flip on any path that has not first observed a valid CDP
	// version response AND produced a real, non-empty screenshot over the
	// exact code path an invocation uses.
	logger.Info("ember-shotter-init: snapshot ordering assertion, trial render confirmed before readiness flip")
	ready.Store(true)
	logger.Info("ember-shotter-init: readiness flipped, warm and render-tested Chromium base ready")

	return waitForShutdown(ctx, serveErr, proxyErr, chromiumExit, logger)
}

func chromiumArgv() []string {
	return []string{
		chromiumBinary,
		// Use Chromium's current headless implementation, which shares the full
		// browser rendering path used by normal Chrome.
		"--headless=new",
		// The guest has no user namespaces. Chromium runs as uid 65532, and the
		// microVM boundary supplies isolation, so its process sandbox is disabled.
		"--no-sandbox",
		// /dev/shm is not provisioned for the guest. Put shared-memory scratch on
		// the deliberately sized /tmp tmpfs captured by the snapshot.
		"--disable-dev-shm-usage",
		// No physical GPU is exposed. Chromium's bundled software rendering stack
		// is sufficient and avoids a system GL dependency.
		"--disable-gpu",
		// CDP is a guest-local control surface. Both the address and port are
		// explicit so it can never bind to 0.0.0.0.
		"--remote-debugging-address=" + cdpAddress,
		fmt.Sprintf("--remote-debugging-port=%d", cdpPort),
		// The in-guest egress proxy (proxy.go) is the only browser egress path;
		// its hard allowlist is the primary control (ADR embervm/035 section 4).
		// Reuse proxyListenAddress rather than a second copy of the literal, so
		// the two can never drift apart.
		"--proxy-server=http://" + proxyListenAddress,
		// Chromium bypasses a configured proxy for loopback by default in most
		// builds, but this makes it explicit rather than relying on that
		// default: a rendered page must never be able to reach the CDP control
		// port at 127.0.0.1:9222 by routing around the proxy's allowlist. This
		// costs nothing because the Go CDP client (cdp.go) dials CDP directly,
		// not through this proxy.
		"--proxy-bypass-list=<-loopback>",
		// All mutable profile data must live in snapshot-backed tmpfs because the
		// rootfs is read-only and shared by clones.
		"--user-data-dir=" + chromiumProfile,
		"about:blank",
	}
}

// trialRender drives one real screenshot of about:blank over CDP, the exact
// T3 capture path (captureScreenshotViaCDP in cdp.go) a real invocation
// uses, so warm-up exercises the same code and catches the same failures a
// caller would hit later. Readiness only flips once this produces
// non-empty PNG bytes: answering /json/version proves the CDP protocol is
// alive, not that Chromium can actually navigate and rasterize.
func trialRender(ctx context.Context, logger *slog.Logger) error {
	cdpHTTPBase := fmt.Sprintf("http://%s:%d", cdpAddress, cdpPort)
	req := validatedScreenshot{
		navigateURL:     "about:blank",
		width:           defaultWidth,
		height:          defaultHeight,
		fullPage:        false,
		waitUntilEvent:  waitUntilEvents[defaultWaitUntil],
		navigateTimeout: trialRenderTimeout,
	}
	trialCtx, cancel := context.WithTimeout(ctx, trialRenderTimeout)
	defer cancel()
	resp, err := captureScreenshotViaCDP(trialCtx, logger, cdpHTTPBase, req)
	if err != nil {
		return fmt.Errorf("trial capture failed: %w", err)
	}
	if len(resp.PNGBase64) == 0 {
		return errors.New("trial capture produced no PNG bytes")
	}
	return nil
}

func launchChromium(ctx context.Context, logger *slog.Logger) (<-chan error, error) {
	if err := os.MkdirAll(chromiumHome, 0o700); err != nil {
		return nil, fmt.Errorf("create Chromium home: %w", err)
	}
	if uid := os.Geteuid(); uid == 0 {
		if err := os.Chown(chromiumHome, browserUID, browserGID); err != nil {
			return nil, fmt.Errorf("chown Chromium home: %w", err)
		}
	} else if uid != browserUID {
		return nil, fmt.Errorf("guest-init uid is %d, want root or %d", uid, browserUID)
	}

	argv := chromiumArgv()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = append(os.Environ(), "HOME="+chromiumHome)
	// Attach files directly to the guest console. Do not use StdoutPipe,
	// StderrPipe, or CombinedOutput for this long-lived process: an open pipe
	// would keep the warm phase blocked until Chromium exits.
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	setBrowserCredential(cmd)

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start Chromium: %w", err)
	}
	logger.Info("ember-shotter-init: warm ordering checkpoint 1, Chromium started", "pid", cmd.Process.Pid, "uid", browserUID, "argv", argv)

	exited := make(chan error, 1)
	go func() {
		exited <- cmd.Wait()
		close(exited)
	}()
	return exited, nil
}

type cdpVersion struct {
	Browser              string `json:"Browser"`
	WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
}

func waitForCDP(ctx context.Context, chromiumExit <-chan error, logger *slog.Logger) error {
	warmCtx, cancel := context.WithTimeout(ctx, cdpWarmTimeout)
	defer cancel()

	url := fmt.Sprintf("http://%s:%d/json/version", cdpAddress, cdpPort)
	client := &http.Client{
		Timeout: cdpProbeTimeout,
		Transport: &http.Transport{
			Proxy: nil,
		},
	}
	defer client.CloseIdleConnections()

	var lastErr error
	for {
		version, err := probeCDP(warmCtx, client, url)
		if err == nil {
			logger.Info("ember-shotter-init: CDP endpoint ready", "browser", version.Browser, "url", url)
			return nil
		}
		lastErr = err

		select {
		case err := <-chromiumExit:
			return fmt.Errorf("Chromium exited before CDP became ready: %v", err)
		case <-time.After(cdpPollInterval):
		case <-warmCtx.Done():
			return fmt.Errorf("CDP did not become ready within %s, last probe: %w", cdpWarmTimeout, lastErr)
		}
	}
}

func probeCDP(ctx context.Context, client *http.Client, url string) (cdpVersion, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return cdpVersion{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return cdpVersion{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return cdpVersion{}, fmt.Errorf("CDP status %s", resp.Status)
	}
	var version cdpVersion
	if err := json.NewDecoder(resp.Body).Decode(&version); err != nil {
		return cdpVersion{}, fmt.Errorf("decode CDP version: %w", err)
	}
	if version.Browser == "" || version.WebSocketDebuggerURL == "" {
		return cdpVersion{}, fmt.Errorf("CDP version response omitted browser identity or websocket URL")
	}
	return version, nil
}

func newMux(ready func() bool, logger *slog.Logger) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /shim/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	var firstPoll, firstReady sync.Once
	mux.HandleFunc("GET /shim/ready", func(w http.ResponseWriter, _ *http.Request) {
		firstPoll.Do(func() {
			logger.Info("ember-shotter-init: first /shim/ready poll received")
		})
		if !ready() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		firstReady.Do(func() {
			logger.Info("ember-shotter-init: first /shim/ready answered 200, noded may snapshot now")
		})
		w.WriteHeader(http.StatusOK)
	})

	mux.HandleFunc("POST /shim/clock", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			EpochMs int64 `json:"epoch_ms"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, fmt.Sprintf("bad clock request: %s", err), http.StatusBadRequest)
			return
		}
		if body.EpochMs <= 0 {
			http.Error(w, "epoch_ms must be positive", http.StatusBadRequest)
			return
		}
		if err := setWallClock(body.EpochMs); err != nil {
			logger.Warn("ember-shotter-init: guest clock resync failed", "epoch_ms", body.EpochMs, "err", err)
			http.Error(w, fmt.Sprintf("set clock failed: %s", err), http.StatusInternalServerError)
			return
		}
		logger.Info("ember-shotter-init: guest clock resynced", "epoch_ms", body.EpochMs)
		w.WriteHeader(http.StatusNoContent)
	})

	// screenshotHandler (screenshot.go) drives the already warm Chromium over
	// CDP: fresh target per invocation, navigate, wait, capture, close.
	mux.HandleFunc("POST "+screenshotPath, screenshotHandler(logger))
	return mux
}

func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"HOME": chromiumHome,
		"PATH": "/usr/local/bin:/usr/bin:/bin",
	}
	for key, value := range defaults {
		if _, ok := os.LookupEnv(key); ok {
			continue
		}
		if err := os.Setenv(key, value); err != nil {
			logger.Warn("ember-shotter-init: could not set default environment", "key", key, "err", err)
		}
	}
}

func waitForShutdown(ctx context.Context, serveErr, proxyErr, chromiumExit <-chan error, logger *slog.Logger) error {
	select {
	case <-ctx.Done():
		logger.Info("ember-shotter-init: shutdown signal")
		return nil
	case err := <-serveErr:
		if err != nil {
			return fmt.Errorf("vsock server stopped: %w", err)
		}
		return nil
	case err := <-proxyErr:
		if err != nil {
			return fmt.Errorf("egress proxy stopped: %w", err)
		}
		return nil
	case err := <-chromiumExit:
		return fmt.Errorf("chromium exited while guest was serving: %v", err)
	}
}

const guestHTTPPort = vsockproto.GuestHTTPPort
