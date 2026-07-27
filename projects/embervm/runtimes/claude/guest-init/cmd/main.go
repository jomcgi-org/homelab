// Command ember-runtime-guest-init is PID 1 of the Claude runtime microVM.
// Raw Firecracker boots ignore the OCI entrypoint, so this init prepares the
// writable guest paths and execs the Claude shim.
package main

import (
	"log/slog"
	"os"
	"strings"
)

var shimCmd = []string{"/usr/local/bin/ember-claude-shim"}

var procCmdlinePath = "/proc/cmdline"

const workspaceDevCmdlineKey = "ember.workspace_dev"

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-runtime-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	mountTmpfsTmp(logger)
	mountProc(logger)
	setDefaultEnv(logger)
	mountWorkspaceVolume(logger)
	return execShim(logger)
}

// valueFromCmdline extracts the value of a key=value token. The last
// occurrence wins, matching the kernel's handling of duplicate cmdline keys.
func valueFromCmdline(cmdline, key string) string {
	value := ""
	for _, tok := range strings.Fields(cmdline) {
		k, val, ok := strings.Cut(tok, "=")
		if ok && k == key && val != "" {
			value = val
		}
	}
	return value
}

func isValidEnvKeyName(key string) bool {
	if key == "" {
		return false
	}
	for _, r := range key {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			continue
		}
		return false
	}
	return true
}

func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH":                   "/usr/bin:/bin:/usr/local/bin",
		"HOME":                   "/home/runtime",
		"PYTHONUNBUFFERED":       "1",
		"TERM":                   "dumb",
		"EMBER_CLAUDE_WORKSPACE": "/workspace",
	}
	for key, value := range defaults {
		if _, set := os.LookupEnv(key); set {
			continue
		}
		if err := os.Setenv(key, value); err != nil {
			logger.Warn("could not set default env", "key", key, "err", err)
		}
	}
}
