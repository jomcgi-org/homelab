//go:build !linux

package main

import "log/slog"

func mountTmpfsTmp(*slog.Logger) {}
func mountProc(*slog.Logger)     {}
