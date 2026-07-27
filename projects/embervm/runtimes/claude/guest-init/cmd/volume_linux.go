//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"

	"golang.org/x/sys/unix"
)

const (
	workspaceMountPath = "/workspace"
	runtimeHomePath    = "/home/runtime"
)

func mountWorkspaceVolume(logger *slog.Logger) {
	devicePresent := false
	raw, err := os.ReadFile(procCmdlinePath)
	if err == nil {
		if dev := valueFromCmdline(string(raw), workspaceDevCmdlineKey); dev != "" {
			devicePresent = true
			if err := mountVolumeDevice(logger, dev, workspaceMountPath); err == nil {
				logger.Info("workspace volume: mounted, device=" + dev)
				ensureRuntimeHome(logger)
				return
			} else {
				logger.Warn("workspace volume: device unavailable, falling back to tmpfs", "device", dev, "err", err)
			}
		}
	}

	if err := os.MkdirAll(workspaceMountPath, 0o755); err != nil {
		logger.Warn("workspace volume: could not create mount path", "err", err)
	} else if err := unix.Mount("tmpfs", workspaceMountPath, "tmpfs", 0, "size=256m,mode=755"); err != nil {
		logger.Warn("workspace volume: tmpfs mount failed", "err", err)
	} else if devicePresent {
		logger.Info("workspace volume: using tmpfs, device unavailable")
	} else {
		logger.Info("workspace volume: using tmpfs, no device attached")
	}
	ensureRuntimeHome(logger)
}

func ensureRuntimeHome(logger *slog.Logger) {
	if err := os.MkdirAll(runtimeHomePath, 0o755); err != nil {
		logger.Warn("home: could not create runtime home", "err", err)
		return
	}
	if err := os.Chown(runtimeHomePath, 65532, 65532); err != nil {
		logger.Warn("home: could not set runtime ownership", "err", err)
	}
	if err := os.Chmod(runtimeHomePath, 0o755); err != nil {
		logger.Warn("home: could not set runtime permissions", "err", err)
	}
	logger.Info("home: runtime writable", "path", runtimeHomePath, "uid", 65532)
}

func mountVolumeDevice(logger *slog.Logger, dev, mountPath string) error {
	blank, err := deviceIsBlank(dev)
	if err != nil {
		return fmt.Errorf("probe workspace device %s: %w", dev, err)
	}
	if blank {
		logger.Info("workspace volume: no filesystem signature, formatting ext4", "device", dev)
		if out, err := exec.Command("mkfs.ext4", "-q", dev).CombinedOutput(); err != nil {
			return fmt.Errorf("mkfs.ext4 %s: %w: %s", dev, err, string(out))
		}
	}
	if err := os.MkdirAll(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir workspace mount path %s: %w", mountPath, err)
	}
	if err := unix.Mount(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	return nil
}

func deviceIsBlank(dev string) (bool, error) {
	out, err := exec.Command("blkid", "-o", "value", "-s", "TYPE", dev).CombinedOutput()
	if err == nil {
		return len(trimTrailingNewline(out)) == 0, nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok && exitErr.ExitCode() == 2 {
		return true, nil
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}

func trimTrailingNewline(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		return b[:n-1]
	}
	return b
}
