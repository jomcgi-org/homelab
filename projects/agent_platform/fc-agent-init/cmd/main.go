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
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/harness"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/idle"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/reconnect"
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

	// Launch the harness. The command is the wrapper's argv after a "--", or the
	// FC_HARNESS_CMD env. With neither, idle/reconnect still run (useful for a
	// warm-base boot that has no task yet).
	harness := harnessCommand()
	var harnessProc *exec.Cmd
	if len(harness) > 0 {
		harnessProc = exec.CommandContext(ctx, harness[0], harness[1:]...)
		harnessProc.Stdout = os.Stdout
		harnessProc.Stderr = os.Stderr
		harnessProc.Env = os.Environ()
		if err := harnessProc.Start(); err != nil {
			return err
		}
		logger.Info("harness started", "argv", harness)
	}

	// Connect to the controller. Without a transport configured (off-cluster),
	// run the idle loop without a channel so the binary still exercises cleanly.
	conn := dialController(logger)
	if conn != nil {
		defer conn.Close()
		go serveControl(ctx, logger, conn, rec, threadID)
	}

	go det.Run(ctx, time.Second, func(wake vsockproto.WakeCondition) {
		logger.Info("idle boundary reached; signalling controller", "wake", string(wake))
		if conn != nil {
			if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindIdle, ThreadID: threadID, Wake: wake}); err != nil {
				logger.Warn("failed to send idle signal", "err", err)
			}
		}
	})

	// Block until shutdown or the harness exits.
	if harnessProc != nil {
		err := harnessProc.Wait()
		logger.Info("harness exited", "err", err)
		return nil
	}
	<-ctx.Done()
	return nil
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

// dialController returns a framed connection to the host controller, or nil if
// no transport is configured. Real vsock dialing is host-kernel specific and is
// finalized in Phase 5; until then this is a no-op off-cluster.
func dialController(logger *slog.Logger) *vsockproto.Conn {
	if os.Getenv("FC_CONTROLLER_VSOCK") == "" {
		logger.Info("no controller transport configured; idle/reconnect run locally")
		return nil
	}
	logger.Warn("vsock transport configured but dialing is finalized in Phase 5")
	return nil
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
