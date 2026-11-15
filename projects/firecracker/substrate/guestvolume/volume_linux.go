//go:build linux

// Package guestvolume prepares an explicitly selected guest ext4 volume.
// Callers own device selection, restore decisions, and logging.
package guestvolume

import (
	"fmt"
	"os"
	"os/exec"
)

// Operations supplies all side effects per call. CombinedOutput must include
// stderr, matching exec.Cmd.CombinedOutput. Tests supply only fake operations.
type Operations struct {
	CombinedOutput func(string, ...string) ([]byte, error)
	MkdirAll       func(string, os.FileMode) error
	Mount          func(string, string, string, uintptr, string) error
}

// Hooks preserves caller logging at each successful transition. AfterFormat
// runs only after mkfs succeeds; Mounted runs only after mount succeeds.
type Hooks struct {
	BeforeFormat func()
	AfterFormat  func()
	Mounted      func()
}

// Mount probes dev, optionally formats it, creates mountPath, then mounts ext4.
// mkfsArgs is the caller's complete mkfs.ext4 argv, including the device.
// The first failure stops the sequence. There are no default side effects.
func Mount(dev, mountPath string, mkfsArgs []string, ops Operations, hooks Hooks) error {
	blank, err := deviceIsBlank(dev, ops.CombinedOutput)
	if err != nil {
		return fmt.Errorf("probe volume device %s: %w", dev, err)
	}
	if blank {
		if hooks.BeforeFormat != nil {
			hooks.BeforeFormat()
		}
		if out, err := ops.CombinedOutput("mkfs.ext4", mkfsArgs...); err != nil {
			return fmt.Errorf("mkfs.ext4 %s: %w: %s", dev, err, string(out))
		}
		if hooks.AfterFormat != nil {
			hooks.AfterFormat()
		}
	}
	if err := ops.MkdirAll(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir volume mount path %s: %w", mountPath, err)
	}
	if err := ops.Mount(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	if hooks.Mounted != nil {
		hooks.Mounted()
	}
	return nil
}

func deviceIsBlank(dev string, combinedOutput func(string, ...string) ([]byte, error)) (bool, error) {
	out, err := combinedOutput("blkid", "-o", "value", "-s", "TYPE", dev)
	if err == nil {
		// Preserve the retained guests' policy: remove exactly one LF. CRLF
		// and two LFs still count as signatures.
		if n := len(out); n > 0 && out[n-1] == '\n' {
			out = out[:n-1]
		}
		return len(out) == 0, nil
	}
	// Only a concrete ExitError with code 2 means blank, even with output.
	// Do not broaden this to wrapped exit errors or other probe failures.
	if exitErr, ok := err.(*exec.ExitError); ok && exitErr.ExitCode() == 2 {
		return true, nil
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}
