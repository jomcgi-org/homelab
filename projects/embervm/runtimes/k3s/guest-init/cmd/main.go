// Command ember-k3s-init is the PID 1 of the EmberVM composite (R5) k3s guest
// microVMs (ADR embervm/001, plan Task 3). ONE init binary serves both the
// k3s-server and the k3s-agent images; the role is data, read from the injected
// EMBER_GROUP_ROLE env, not baked per image.
//
// A raw Firecracker boot ignores the OCI image config entirely and boots
// init=<HarnessInit> (see the noded driver's bootArgs), so the apko entrypoint
// is never honoured. This init is that missing PID 1. On boot it:
//
//  1. mounts the writable filesystems k3s needs on the read-only, snapshot-shared
//     rootfs (tmpfs over /run, /var/log, /var/lib/rancher's writable subtree, and
//     /tmp), plus /proc / /sys so k3s and the boot-arg readers work;
//  2. sets PATH plus the k3s defaults a raw boot lacks;
//  3. decodes the MMDS-lite boot-arg env (ember.env.<KEY>=<base64url>) into the
//     process env: for the SPIKE the EMBER_GROUP_* facts arrive through the
//     serving lane's mmds_env seam (no group machinery exists yet, plan Task 3
//     scope), and translate to the same env either way (standing decision 13);
//  4. reads ember.serving_port into EMBER_SERVING_PORT (the serving lane's health
//     port signal), informational here since k3s binds its own fixed ports;
//  5. starts the vsock guest control agent (guestagent) as a supervised sidecar
//     goroutine on the frozen port 1024, so a post-resume clock resync works;
//  6. maps the EMBER_GROUP_* facts to k3s flags (standing decision 13) and
//     supervises `k3s server ...` (role server) or `k3s agent ...` (role agent).
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
	"syscall"
)

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
	// baked (read-only) subdir of the same tree, so the tmpfs is layered over the
	// writable children only. /proc + /sys are required by k3s and by the boot-arg
	// readers. Best-effort per mount (logged, not fatal): a missing mount surfaces
	// as a k3s startup failure downstream, which is the honest place for it.
	mountGuestFilesystems(logger)

	// A raw FC boot hands PID 1 no environment. Set PATH + k3s defaults, matching
	// the apko image `environment` block (which a Firecracker boot never consumes).
	setDefaultEnv(logger)

	// MMDS-lite first-boot facts (D-R4.PR-7.1): decode every
	// ember.env.<KEY>=<base64url> boot-arg into the process env BEFORE the k3s
	// flag mapping reads EMBER_GROUP_*. For the spike these arrive via the serving
	// lane; under the group machinery (Task 4+) via the same seam. A boot with no
	// such tokens leaves the env unchanged.
	setMmdsEnv(logger)

	// Serving-lane health-port signal (R3): translate ember.serving_port into
	// EMBER_SERVING_PORT. k3s binds its own fixed ports (6443 server, 10250
	// agent), so this is informational; noded health-gates the tap-NIC port it
	// chose, which for the spike CR is 6443.
	setServingPortEnv(logger)

	// Start the vsock guest control agent (guestagent) sidecar: a goroutine
	// listening on the frozen port 1024 that answers sync_clock so a post-resume
	// clock resync works (standing decision 7). Non-fatal off Linux / no vsock, so
	// the host build still runs. It is a SUPERVISED sidecar of this init: it lives
	// for the guest's lifetime alongside k3s, never replaces it.
	startGuestAgent(ctx, logger)

	// Map EMBER_GROUP_* -> k3s flags (standing decision 13) and supervise k3s.
	// This blocks until k3s exits or the context is cancelled (SIGTERM). k3s is
	// fork+exec (not exec-replace) because the guest agent goroutine must keep
	// running alongside it; this init is the supervisor.
	return superviseK3s(ctx, logger)
}
