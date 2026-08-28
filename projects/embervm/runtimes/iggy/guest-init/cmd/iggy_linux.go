//go:build linux

package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"time"
)

// iggyServerBinary is the static musl iggy-server lifted out of the pinned
// apache/iggy image (//bazel/tools/oci:oci_binaries) and layered here by
// ../../BUILD.
const iggyServerBinary = "/usr/local/bin/iggy-server"

// readyDeadline bounds the wait for the server to accept TCP. It sits just under
// the CR's wakeTimeoutSeconds so a server that never comes up surfaces here
// (named, in the guest log) rather than only as a wake timeout on the host.
const readyDeadline = 55 * time.Second

// launchIggy owns the stateful data path. mountPath is the mounted volume (e.g.
// /data); the server's data root lives at <mountPath>/iggy so the volume root can
// hold other state without colliding with the server's own layout.
//
// There is no separate bootstrap step to run: unlike Postgres's initdb,
// iggy-server creates its system info, root user, and empty stream set in-process
// on a first boot and loads them on every later boot, idempotently. So this
// function only has to prepare the directory, refuse a first boot with no root
// password, launch the server, and flip ready once it accepts TCP.
//
// The server runs as a child (not exec) so the vsock ready server keeps serving;
// this init stays PID 1.
func launchIggy(ctx context.Context, logger *slog.Logger, mountPath string, ready *atomic.Bool) error {
	systemPath := iggySystemPath(mountPath)

	// The volume is owned by root after mkfs+mount; hand the mount point and the
	// data root to the iggy uid so the server can write.
	if err := os.MkdirAll(systemPath, 0o750); err != nil {
		return fmt.Errorf("mkdir iggy system path %s: %w", systemPath, err)
	}
	if err := chownRecursive(mountPath, iggyUID, iggyGID); err != nil {
		return fmt.Errorf("chown volume %s to iggy: %w", mountPath, err)
	}

	// Probed once and reused: the password gate and the log line below must agree
	// about whether this is a first boot.
	bootstrapped, err := stateBootstrapped(systemPath)
	if err != nil {
		return fmt.Errorf("probe iggy state log under %s: %w", systemPath, err)
	}
	if err := requireRootPassword(bootstrapped); err != nil {
		return err
	}

	if bootstrapped {
		logger.Info("stateful iggy: volume already bootstrapped, loading existing state", "path", systemPath)
	} else {
		logger.Info("stateful iggy: empty volume, server will bootstrap its state on start", "path", systemPath)
	}

	cmd := iggyCommand(ctx, mountPath)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start iggy-server: %w", err)
	}
	logger.Info("stateful iggy: launched", "path", systemPath, "port", iggyTCPPort)

	go func() {
		if err := waitIggyReady(ctx); err != nil {
			logger.Warn("stateful iggy: readiness wait failed", "err", err)
			return
		}
		ready.Store(true)
		logger.Info("stateful iggy: ready (accepting TCP)")
	}()

	// Reap the server child. If it exits, that is a hard failure of the stateful
	// guest; return so run surfaces it (noded's TCP probe will already have failed
	// the wake).
	return cmd.Wait()
}

// iggyCommand builds the iggy-server invocation, dropped to the iggy uid, with
// its data root on the mounted volume. Every other knob (bind address, disabled
// transports, file logging) is an IGGY_* env var rather than a flag, because that
// is the only override seam iggy-server offers for config values; see
// setDefaultEnv.
//
// Dir is the volume mount rather than the read-only rootfs: iggy-server probes
// for `core/server/config.toml` relative to the working directory and falls back
// to the config compiled into the binary when it finds none. Pointing Dir at a
// directory that provably has no such file keeps that fallback deterministic, and
// keeps any other relative path the server might touch off the read-only rootfs.
func iggyCommand(ctx context.Context, mountPath string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, iggyServerBinary)
	cmd.Dir = mountPath
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = iggyChildEnv(mountPath)
	dropToIggy(cmd)
	return cmd
}

// waitIggyReady polls a local TCP connect to iggyTCPPort until it succeeds or the
// context is cancelled, so the ready flip only happens once the server actually
// accepts connections.
func waitIggyReady(ctx context.Context) error {
	deadline := time.Now().Add(readyDeadline)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", iggyTCPPortString), time.Second)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("iggy-server did not accept TCP within deadline: %w", err)
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// dropToIggy sets the command's SysProcAttr so it runs as the iggy uid/gid rather
// than root.
func dropToIggy(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Credential = &syscall.Credential{Uid: iggyUID, Gid: iggyGID}
}

// chownRecursive chowns path and everything under it to uid/gid. Used once to
// hand the freshly mkfs'd volume mount to the iggy uid.
func chownRecursive(path string, uid, gid int) error {
	return filepath.Walk(path, func(name string, _ os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		return os.Chown(name, uid, gid)
	})
}
