// Command ember-runtime-guest-init is PID 1 of the Claude runtime microVM.
// Raw Firecracker boots ignore the OCI entrypoint, so this init prepares the
// writable guest paths and execs the Claude shim.
package main

import (
	"encoding/base64"
	"log/slog"
	"os"
	"strings"
)

var shimCmd = []string{"/usr/local/bin/ember-claude-shim"}

var procCmdlinePath = "/proc/cmdline"

const (
	// The keys noded ACTUALLY emits (fcvm/driver bootArgsFor). This guest used to
	// read ember.workspace_dev, which noded has never emitted, so a drive attached
	// through the existing volume path would have been silently ignored: both
	// halves looked wired in isolation and neither could reach the other. Reuse
	// noded's convention rather than teach the daemon a second one.
	//
	// volume_dev is the actual device, NOT a fixed /dev/vdc: drives land on
	// /dev/vd{a,b,c...} in attach order, so a boot with no handler disk (which is
	// every session boot) puts the writable volume on vdb.
	volumeDevCmdlineKey   = "ember.volume_dev"
	volumeMountCmdlineKey = "ember.volume_mount"
	mmdsEnvCmdlinePrefix  = "ember.env."
)

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
	bringUpLoopback(logger)
	setDefaultEnv(logger)
	setMmdsEnv(logger)
	if err := mountWorkspaceVolume(logger); err != nil {
		return err
	}
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

// setMmdsEnv applies environment values supplied by noded as
// ember.env.<KEY>=<base64url> kernel arguments. Individual malformed tokens are
// skipped so one bad secret does not prevent other valid values from reaching
// the shim. Values are never logged.
func setMmdsEnv(logger *slog.Logger) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return
	}
	keys := make([]string, 0)
	for _, tok := range strings.Fields(string(raw)) {
		name, encoded, ok := strings.Cut(tok, "=")
		if !ok || encoded == "" || !strings.HasPrefix(name, mmdsEnvCmdlinePrefix) {
			continue
		}
		key := strings.TrimPrefix(name, mmdsEnvCmdlinePrefix)
		if !isValidEnvKeyName(key) {
			logger.Warn("skipping invalid mmds env key", "key", key)
			continue
		}
		value, err := base64.RawURLEncoding.DecodeString(encoded)
		if err != nil {
			logger.Warn("skipping malformed mmds env value", "key", key)
			continue
		}
		if err := os.Setenv(key, string(value)); err != nil {
			logger.Warn("could not set mmds env var", "key", key, "err", err)
			continue
		}
		keys = append(keys, key)
	}
	if len(keys) > 0 {
		logger.Info("set mmds env vars from boot args", "keys", keys)
	}
}

func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH":                   "/usr/bin:/bin:/usr/local/bin",
		"HOME":                   "/home/runtime",
		"PYTHONUNBUFFERED":       "1",
		"TERM":                   "dumb",
		"EMBER_CLAUDE_WORKSPACE": "/workspace",
		// The shim treats a committer identity as mandatory and fails a spawn
		// without one, so every turn would 503 on a guest that has none. A
		// session-class guest is restored from a SHARED pristine snapshot, so
		// there is no per-session boot to carry a per-session identity: boot-args
		// are consumed once, at base-build time, for every session alike.
		//
		// So default to a service identity here and let setMmdsEnv override it,
		// which keeps the guest bootable today without pretending the value is
		// per-principal. Attributing a commit to the human who asked for it is
		// per-commit --author, tracked with the rest of git integration in #4070.
		"EMBER_GIT_USER_NAME":  "EmberVM Agent",
		"EMBER_GIT_USER_EMAIL": "agent@jomcgi.dev",
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
