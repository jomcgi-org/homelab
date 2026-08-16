//go:build !linux

package main

import (
	"fmt"
	"log/slog"
	"os/exec"
)

const (
	browserUID = 65532
	browserGID = 65532
)

func bringUpLoopback(_ *slog.Logger) {}
func setHostname(_ *slog.Logger)     {}

func setWallClock(int64) error {
	return fmt.Errorf("setWallClock is only supported on linux")
}

func setBrowserCredential(_ *exec.Cmd) {}
