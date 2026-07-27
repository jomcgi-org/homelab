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
	workspaceTmpfsSize = "size=2g,mode=755"
)

var (
	workspaceMountPath  = "/workspace"
	runtimeHomePath     = "/home/runtime"
	mountFn             = unix.Mount
	mountVolumeDeviceFn = mountVolumeDevice
	mkdirAllFn          = os.MkdirAll
	chownFn             = os.Chown
	chmodFn             = os.Chmod
)

func mountWorkspaceVolume(logger *slog.Logger) error {
	raw, err := os.ReadFile(procCmdlinePath)
	if err == nil {
		if dev := valueFromCmdline(string(raw), workspaceDevCmdlineKey); dev != "" {
			// If workspace_dev is present, fail closed: a guest cannot run correctly
			// without its persistent volume.
			if err := mountVolumeDeviceFn(logger, dev, workspaceMountPath); err != nil {
				return fmt.Errorf("mount requested workspace device %s: %w", dev, err)
			}
			logger.Info("workspace volume: mounted", "device", dev)
			return ensureRuntimeHome()
		}
	}

	if err := mkdirAllFn(workspaceMountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir workspace mount path %s: %w", workspaceMountPath, err)
	}
	// A tmpfs workspace is only valid for a base build with no persistent device.
	// Two GiB covers typical repository checkouts and git working-tree overhead.
	if err := mountFn("tmpfs", workspaceMountPath, "tmpfs", 0, workspaceTmpfsSize); err != nil {
		return fmt.Errorf("mount tmpfs workspace: %w", err)
	}
	logger.Info("workspace volume: using tmpfs, no device attached")
	return ensureRuntimeHome()
}

func ensureRuntimeHome() error {
	if err := mkdirAllFn(runtimeHomePath, 0o755); err != nil {
		return fmt.Errorf("mkdir runtime home %s: %w", runtimeHomePath, err)
	}
	if err := chownFn(runtimeHomePath, 65532, 65532); err != nil {
		return fmt.Errorf("chown runtime home %s: %w", runtimeHomePath, err)
	}
	if err := chmodFn(runtimeHomePath, 0o755); err != nil {
		return fmt.Errorf("chmod runtime home %s: %w", runtimeHomePath, err)
	}
	return nil
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
	if err := mkdirAllFn(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir workspace mount path %s: %w", mountPath, err)
	}
	if err := mountFn(dev, mountPath, "ext4", 0, ""); err != nil {
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
