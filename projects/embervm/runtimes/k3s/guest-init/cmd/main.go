// Command ember-k3s-init is the PID 1 of the EmberVM composite (R5) k3s guest
// microVMs (ADR embervm/001, plan Task 3). ONE init binary serves both the
// k3s-server and the k3s-agent images; the role is data, read from the injected
// EMBER_GROUP_ROLE env (defaulting to server when unset, the factless spike).
//
// A raw Firecracker boot ignores the OCI image config entirely and boots
// init=<HarnessInit> (see the noded driver's bootArgs), so the apko entrypoint
// is never honoured. This init is that missing PID 1. Like the scratch-postgres
// guest-init it has to satisfy TWO distinct boot classes off ONE rootfs:
//
//   - BASE BUILD (a plain cold boot with NO volume boot-arg): noded's BuildBase
//     cold-boots the guest, health-gates it over vsock at the frozen readiness
//     contract (GET /shim/ready on GuestHTTPPort 1027), snapshots the warm base,
//     and discards the VM. Here there is no volume and no k3s to run: this init
//     answers the vsock ready contract (200 immediately, the warm base is just
//     the OS + the baked k3s binary + airgap tarball) so the base snapshot is
//     taken and the image becomes cold-bootable. k3s first runs on the cold boot.
//
//   - STATEFUL COLD BOOT (a cold boot carrying ember.volume_dev / ember.env.*):
//     the k3s guest boots on the stateful lane (scratch-postgres precedent, the
//     exact lane for a full non-shim image with a tap-NIC TCP health-gate and an
//     mmds_env secret seam). guest-init mounts the writable volume at
//     ember.volume_mount (k3s's data dir), decodes the MMDS-lite EMBER_GROUP_*
//     facts (empty for the factless single-server spike), starts the vsock clock
//     agent, maps the facts to k3s flags (standing decision 13), and supervises
//     `k3s server`/`k3s agent`. Runtime health is a TCP connect to the k3s port
//     over the tap NIC (noded's finishStatefulStart), NOT the vsock ready path;
//     the vsock server stays up harmlessly.
//
// The airgap image tarball baked at /var/lib/rancher/k3s/agent/images is imported
// by k3s itself at startup (standing decision 12: zero-egress). This init never
// pulls anything.
//
// In-guest ROOT posture: k3s (containerd, CNI, iptables, cgroup mounts) requires
// root inside the guest. The microVM boundary is the isolation statement (task-
// class posture); this deviation from the uid 65532 convention is documented in
// the image README rather than being silent.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
)

// readyFlag is the readiness gate the vsock /shim/ready probe polls. A thin
// wrapper over atomic.Bool so its Load method matches shim.WithReady's func()
// bool signature directly.
type readyFlag struct{ atomic.Bool }

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-k3s-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Writable filesystems on the read-only rootfs. k3s writes to /run (containerd
	// socket, CNI state), /var/log, and the writable subtree of
	// /var/lib/rancher/k3s (server db, kubelet); the airgap tarball ships in a
	// baked (read-only) subdir of the same tree. /proc + /sys are required by k3s
	// and by the boot-arg readers. Best-effort per mount (logged, not fatal).
	mountGuestFilesystems(logger)

	// A raw FC boot hands PID 1 no environment. Set PATH + k3s defaults, matching
	// the apko image `environment` block (which a Firecracker boot never consumes).
	setDefaultEnv(logger)

	// MMDS-lite first-boot facts (D-R4.PR-7.1): decode every
	// ember.env.<KEY>=<base64url> boot-arg into the process env. On the stateful
	// lane these carry the EMBER_GROUP_* facts (from the CR's secretRef); the
	// factless single-server spike carries none, so this is a no-op and the guest
	// defaults to a bare `k3s server`. A base build carries none either.
	setMmdsEnv(logger)

	// Serving-lane health-port signal (informational; k3s binds its own fixed
	// ports). A no-op on the stateful lane and base build.
	setServingPortEnv(logger)

	// Bring up the vsock readiness server first (non-fatal off Linux / no vsock),
	// so BuildBase's WaitReady can always reach the frozen /shim/ready contract on
	// GuestHTTPPort. It serves GET /shim/healthz (200 always) and GET /shim/ready
	// (200 once ready). Mirrors the scratch-postgres guest-init.
	var ready readyFlag
	serveErr := startVsockReadyServer(ctx, logger, ready.Load)

	// Detect the boot class from the presence of the volume boot-arg (exactly as
	// scratch-postgres distinguishes a base build from a stateful cold boot). A
	// base build has none and only needs the vsock ready answer; a stateful cold
	// boot mounts the volume and runs k3s.
	dev, mountPath := statefulVolumeFromCmdline(logger)

	if dev == "" {
		// Base build: no volume, no k3s. Answer ready and hold PID 1 until the
		// host snapshots and reaps the VM. Do NOT exit: exiting PID 1 panics the
		// guest kernel before noded can snapshot.
		ready.Store(true)
		logger.Info("ember-k3s-init: base build boot, ready (no volume, no k3s)")
		return waitForShutdown(ctx, serveErr, logger)
	}

	// Stateful cold boot: mount the writable volume at k3s's data dir BEFORE k3s
	// starts, so the sqlite datastore + kubelet state land on durable storage.
	if err := mountStatefulVolume(logger, dev, mountPath); err != nil {
		return err
	}

	// Start the vsock guest control agent (guestagent) sidecar on the frozen port
	// 1024, so a post-resume clock resync works (standing decision 7). Non-fatal
	// off Linux / no vsock. It is a SUPERVISED sidecar alongside k3s.
	startGuestAgent(ctx, logger)

	// The k3s runtime health is a TCP connect to the k3s port over the tap NIC,
	// not the vsock ready path, but flip vsock ready true anyway so a stray probe
	// reflects that the guest booted its workload (mirrors postgres).
	ready.Store(true)

	// Map EMBER_GROUP_* -> k3s flags (standing decision 13, factless default) and
	// supervise k3s. Blocks until k3s exits or the context is cancelled. k3s is
	// fork+exec (not exec-replace) because the guest agent goroutine must keep
	// running alongside it; this init is the supervisor.
	return superviseK3s(ctx, logger)
}
