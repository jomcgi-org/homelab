// Command fc-agent-init is the in-microVM wrapper from ADR 022: the guest's
// PID 1. It launches the agent harness, watches for a quiescent idle boundary
// and signals the controller over vsock that it is safe to snapshot, and on a
// wake (after restore) re-establishes the guest's connections before handing
// control back to the harness.
//
// Phase 2 wires the components (idle detector, reconnect manager, vsock
// protocol). The host transport is a vsock connection in production; the dial is
// kept behind a small seam so the same wiring is exercised over a pipe in tests.
package main

import (
	"context"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/harness"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/idle"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/reconnect"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/vsockdial"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("fc-agent-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the
	// OCI image config), so establish the baseline the harness needs to find its
	// tools. goose lives in /usr/local/bin; gh/git/node in /usr/bin. HOME anchors
	// goose's config under /home/goose-agent (matching the harness image).
	ensureEnv("PATH", "/usr/local/bin:/usr/bin:/bin:/sbin:/usr/local/sbin")
	ensureEnv("HOME", "/home/goose-agent")
	// The harness image stores recipes here; goose's default search path does not
	// include it, so point goose at it explicitly.
	ensureEnv("GOOSE_RECIPE_PATH", "/home/goose-agent/recipes")

	threadID := os.Getenv("FC_THREAD_ID")
	idleAfter := durationEnv("FC_IDLE_AFTER", 60*time.Second)

	logger.Info("fc-agent-init starting", "thread_id", threadID, "idle_after", idleAfter.String())

	// Idle detector: the harness reports call boundaries via a local control
	// path (Phase 3 wiring); here we construct it and run the sampler.
	det := &idle.Detector{IdleAfter: idleAfter}

	// Reconnect manager: in production these re-open the model/MCP/git clients.
	// Registration of the concrete reconnectors is Phase 5 (it depends on the
	// harness image's client config); the manager + ordering live here.
	rec := &reconnect.Manager{Attempts: 5, Backoff: 200 * time.Millisecond}

	// Connect to the controller over vsock. On cluster this is the guest's only
	// channel to the host; off-cluster (no host listener) it returns nil and we
	// fall back to env/argv so the binary still exercises cleanly.
	conn := dialController(ctx, logger)
	if conn != nil {
		defer conn.Close()
		// Route all harness egress through a local proxy that tunnels to the
		// controller's vsock egress forwarder (ADR 023). The harness never reaches
		// the network directly; its only egress is what the host sidecar allows.
		go runEgressProxy(ctx, logger, guestProxyAddr)
		proxyURL := "http://" + guestProxyAddr
		ensureEnv("HTTP_PROXY", proxyURL)
		ensureEnv("HTTPS_PROXY", proxyURL)
		ensureEnv("http_proxy", proxyURL)
		ensureEnv("https_proxy", proxyURL)
	}

	// Determine the work. A raw FC boot gives PID 1 no env, so the task arrives
	// over vsock (Hello -> Assign). Fall back to env/argv when there is no
	// controller (tests, a warm-base probe with no task yet).
	harnessArgv := assignedHarness(logger, conn, threadID)
	if harnessArgv == nil {
		harnessArgv = harnessCommand()
	}

	var harnessProc *exec.Cmd
	if len(harnessArgv) > 0 {
		harnessProc = exec.CommandContext(ctx, harnessArgv[0], harnessArgv[1:]...)
		harnessProc.Stdout = os.Stdout
		harnessProc.Stderr = os.Stderr
		harnessProc.Env = os.Environ()
		if err := harnessProc.Start(); err != nil {
			return err
		}
		logger.Info("harness started", "argv", harnessArgv)
	}

	if conn != nil {
		go serveControl(ctx, logger, conn, rec, threadID)
		go det.Run(ctx, time.Second, func(wake vsockproto.WakeCondition) {
			logger.Info("idle boundary reached; signalling controller", "wake", string(wake))
			if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindIdle, ThreadID: threadID, Wake: wake}); err != nil {
				logger.Warn("failed to send idle signal", "err", err)
			}
		})
	}

	// Block until shutdown or the harness exits.
	if harnessProc != nil {
		err := harnessProc.Wait()
		logger.Info("harness exited", "err", err)
		if conn != nil {
			status := "ok"
			if err != nil {
				status = err.Error()
			}
			if serr := conn.Send(vsockproto.Message{Kind: vsockproto.KindDone, ThreadID: threadID, Status: status}); serr != nil {
				logger.Warn("failed to send done signal", "err", serr)
			}
		}
		return nil
	}
	<-ctx.Done()
	return nil
}

// assignedHarness performs the control-channel handshake: announce Hello, wait
// for the controller's Assign, and build the harness command from it. Returns
// nil if there is no controller or no usable assignment, so the caller falls
// back to env/argv.
func assignedHarness(logger *slog.Logger, conn *vsockproto.Conn, threadID string) []string {
	if conn == nil {
		return nil
	}
	if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindHello, ThreadID: threadID}); err != nil {
		logger.Warn("failed to announce hello", "err", err)
		return nil
	}
	// Blocking read: the controller replies with Assign immediately after Hello.
	// This is the only reader until serveControl starts, so there is no race.
	msg, err := conn.Recv()
	if err != nil {
		logger.Warn("failed to receive task assignment", "err", err)
		return nil
	}
	if msg.Kind != vsockproto.KindAssign {
		logger.Warn("expected task assignment, got other message", "kind", string(msg.Kind))
		return nil
	}
	// Apply the controller-injected harness env (goose provider/model + the
	// in-cluster model base URL): cluster config the guest cannot hardcode.
	for k, v := range msg.Env {
		_ = os.Setenv(k, v)
	}
	logger.Info("task assignment received", "recipe", msg.Recipe, "env_keys", len(msg.Env))
	return harness.GooseCommand(harness.Config{Recipe: msg.Recipe, Task: msg.Task})
}

// serveControl handles host->guest messages: on a wake, reconnect then ack.
func serveControl(ctx context.Context, logger *slog.Logger, conn *vsockproto.Conn, rec *reconnect.Manager, threadID string) {
	for {
		msg, err := conn.Recv()
		if err != nil {
			return
		}
		switch msg.Kind {
		case vsockproto.KindWake:
			logger.Info("wake received; reconnecting", "wake", string(msg.Wake))
			if rerr := rec.Reconnect(ctx); rerr != nil {
				logger.Error("reconnect failed after wake", "err", rerr)
				continue
			}
			if serr := conn.Send(vsockproto.Message{Kind: vsockproto.KindResumeAck, ThreadID: threadID}); serr != nil {
				logger.Warn("failed to send resume ack", "err", serr)
			}
		case vsockproto.KindHeartbeat:
			_ = conn.Send(vsockproto.Message{Kind: vsockproto.KindHeartbeat, ThreadID: threadID})
		default:
			logger.Debug("ignoring message", "kind", string(msg.Kind))
		}
	}
}

func harnessCommand() []string {
	for i, a := range os.Args {
		if a == "--" {
			return os.Args[i+1:]
		}
	}
	// Goose mode (Plan B): a turn-capped recipe run is the agent harness. The
	// recipe's own max_turns bounds it and the between-turns boundary is what the
	// idle detector snapshots on.
	if g := harness.GooseCommand(harness.Config{
		Recipe: os.Getenv("FC_GOOSE_RECIPE"),
		Task:   os.Getenv("FC_TASK"),
	}); g != nil {
		return g
	}
	if cmd := os.Getenv("FC_HARNESS_CMD"); cmd != "" {
		return []string{"/bin/sh", "-c", cmd}
	}
	return nil
}

// dialController opens the control channel to the host over vsock (host CID 2,
// ControlPort). It retries briefly: the guest can boot to this point before the
// controller has its per-thread listener ready, so connection-refused is normal
// for the first attempts. It returns nil when no controller answers (off-cluster
// or no vsock device), and the caller then runs from env/argv with no channel.
func dialController(ctx context.Context, logger *slog.Logger) *vsockproto.Conn {
	deadline := time.Now().Add(5 * time.Second)
	for {
		rwc, err := vsockdial.Dial(vsockproto.HostCID, vsockproto.ControlPort)
		if err == nil {
			logger.Info("controller control channel connected", "port", vsockproto.ControlPort)
			return vsockproto.NewConn(rwc)
		}
		if time.Now().After(deadline) {
			logger.Info("no controller on vsock; idle/reconnect run locally", "err", err)
			return nil
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// guestProxyAddr is where the in-guest egress proxy listens; the harness's
// HTTP(S)_PROXY points here. Every connection is tunnelled over vsock to the
// host, so the harness has no direct network path.
const guestProxyAddr = "127.0.0.1:3128"

// runEgressProxy accepts local TCP connections (the harness's proxied egress)
// and tunnels each over vsock EgressPort to the controller's forwarder, which
// hands them to the egress-proxy sidecar (ADR 023). It returns when ctx is done.
func runEgressProxy(ctx context.Context, logger *slog.Logger, addr string) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		logger.Warn("egress proxy listen failed; harness egress will fail", "addr", addr, "err", err)
		return
	}
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		local, err := ln.Accept()
		if err != nil {
			if ctx.Err() == nil {
				logger.Warn("egress proxy accept", "err", err)
			}
			return
		}
		go tunnelEgress(logger, local)
	}
}

// tunnelEgress dials the host over vsock and copies bytes both ways until either
// side closes.
func tunnelEgress(logger *slog.Logger, local net.Conn) {
	defer local.Close()
	up, err := vsockdial.Dial(vsockproto.HostCID, vsockproto.EgressPort)
	if err != nil {
		logger.Warn("egress vsock dial", "err", err)
		return
	}
	defer up.Close()
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, local); done <- struct{}{} }()
	go func() { _, _ = io.Copy(local, up); done <- struct{}{} }()
	<-done
}

// ensureEnv sets key to def only when it is unset, so an injected value (e.g. a
// future SandboxTemplate) still wins over the boot-time baseline.
func ensureEnv(key, def string) {
	if os.Getenv(key) == "" {
		_ = os.Setenv(key, def)
	}
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
