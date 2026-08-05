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
	if len(os.Args) > 1 && os.Args[1] == "--ensure-workspace-volume" {
		// Parse optional --device flag: --ensure-workspace-volume [--device /dev/vdb]
		var device string
		for i := 2; i < len(os.Args)-1; i++ {
			if os.Args[i] == "--device" && i+1 < len(os.Args) {
				device = os.Args[i+1]
				break
			}
		}
		if err := ensureWorkspaceVolumeWithDevice(logger, device); err != nil {
			logger.Error("workspace volume ensure failed", "err", err)
			os.Exit(1)
		}
		return
	}
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

// setDefaultEnv is the ONLY thing that gives this guest its environment.
//
// apko's `environment:` block does NOT apply here. It becomes OCI image config,
// which is manifest metadata, and the base rootfs is produced with `crane export`
// (the filesystem alone). A raw Firecracker boot then starts init=<this binary>
// and never reads the OCI config, so anything declared only in apko.yaml is
// absent at runtime. That is why this map duplicates apko's values rather than
// deriving from them: apko covers docker/crane execution, this covers every boot
// that actually happens in the cluster. Adding a variable to apko.yaml alone is
// therefore a silent no-op, which has already produced one live incident.
//
// These are FORCED, not defaulted-if-unset. The kernel hands PID 1 its own HOME
// (`/`), so an if-unset guard left HOME at `/` and every turn 503'd on
// `could not lock config file //.gitconfig: Read-only file system`. Nothing
// legitimately pre-populates this environment, and setMmdsEnv runs AFTER this,
// so per-boot overrides still win.
func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH":                   "/usr/bin:/bin:/usr/local/bin",
		"HOME":                   "/home/runtime",
		"PYTHONUNBUFFERED":       "1",
		"TERM":                   "dumb",
		"EMBER_CLAUDE_WORKSPACE": "/workspace",
		// Egress auth (ADR 023 6b). The CLI talks to the API in CLEARTEXT on
		// purpose: its only route out is the shim's forwarder over a host-local
		// vsock, which has no network segment on it, so the egress-proxy sidecar
		// reads the plaintext request, SETS the Authorization header to the real
		// token, and originates the verified TLS to api.anthropic.com:443 itself.
		//
		// The token below is a LOGIN GATE DUMMY, not a credential and not a
		// placeholder. The CLI refuses to make any request at all until it believes
		// it is logged in (it returns "Not logged in, please run /login" with zero
		// API time), so this must be non-empty; but its value is validated against
		// nothing and is discarded by the sidecar. Deliberately uncoupled from chart
		// config: an earlier design required this to match egress.secrets[] byte for
		// byte, which bought nothing and could only be kept honest by a drift test.
		//
		// The env var NAME is load-bearing twice over: it satisfies that gate, and it
		// selects the OAuth request shape (anthropic-beta: oauth-2025-04-20) that a
		// subscription token requires. If a future CLI starts validating the token's
		// format client-side, this value has to become sk-ant-oat01-shaped junk.
		"ANTHROPIC_BASE_URL":      "http://api.anthropic.com",
		"CLAUDE_CODE_OAUTH_TOKEN": "ember-guest-login-gate-dummy-not-a-credential",
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
		// Key only, never the value: one of these carries the egress placeholder,
		// and a log line that prints credential-shaped values is a habit worth not
		// forming even when today's value is inert.
		if existing, set := os.LookupEnv(key); set && existing != value {
			logger.Info("overriding inherited env", "key", key)
		}
		if err := os.Setenv(key, value); err != nil {
			logger.Warn("could not set default env", "key", key, "err", err)
		}
	}
}
