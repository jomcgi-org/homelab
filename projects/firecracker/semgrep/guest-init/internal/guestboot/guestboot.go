// Package guestboot holds the shared PID-1 boot steps for the semgrep guest
// microVMs: loopback up, a tmpfs over /tmp, the process environment, and the
// offline Pro settings file. Both inits run these before serving the shim: the
// warm scan-server init (cmd, osemgrep-pro mcp --pro, single-file) and the
// full-scan init (cmd-full, semgrep scan --pro, whole-tree interfile). Keeping
// the steps here once means the two inits differ only in what they serve and in
// the tmpfs size they ask for, not in boot logic.
package guestboot

import (
	"log/slog"
	"os"
)

const (
	// DefaultOsemgrepPro is the offline-Pro engine binary baked by engine_tar.
	DefaultOsemgrepPro = "/opt/semgrep/osemgrep-pro"
	// DefaultRulesDir is the merged rule set baked by rules_tar (SEMGREP_SCAN_RULES).
	DefaultRulesDir = "/etc/semgrep/rules"

	guestHome        = "/tmp/sghome"
	settingsFileName = "settings.yml"
)

// offlineSettings is the semgrep settings file the engine reads. All three
// fields are load-bearing: without them the settings fail to decode and the Pro
// engine gate trips. The placeholder api_token unlocks the Pro engine offline
// (presence, not validity, is checked) and never leaves the guest.
const offlineSettings = `anonymous_user_id: 00000000-0000-0000-0000-000000000000
has_shown_metrics_notification: true
api_token: placeholder-not-real
`

// SetProcessEnv sets a standard PATH and a UTF-8 locale. A raw Firecracker boot
// gives PID 1 no environment, so PATH is empty and every execvp fails with
// ENOENT; semgrep-core shells out to `uname` to build its TLS authenticator at
// startup, so an unset PATH crashes it even though uname is installed.
func SetProcessEnv() {
	for k, v := range map[string]string{
		"PATH":   "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"LANG":   "C.UTF-8",
		"LC_ALL": "C.UTF-8",
	} {
		_ = os.Setenv(k, v)
	}
}

// SetupEnv writes the offline settings file into the guest HOME (on the tmpfs)
// and sets the environment the engine reads. It returns the settings file path.
// No SEMGREP_APP_TOKEN is set: the placeholder api_token in the settings file is
// what unlocks the Pro engine offline. Call MountTmpfsTmp first so HOME lands on
// the tmpfs.
func SetupEnv(logger *slog.Logger) (string, error) {
	if err := os.MkdirAll(guestHome, 0o755); err != nil {
		return "", err
	}
	settingsFile := guestHome + "/" + settingsFileName
	if err := os.WriteFile(settingsFile, []byte(offlineSettings), 0o600); err != nil {
		return "", err
	}
	for k, v := range map[string]string{
		"HOME":                         guestHome,
		"SEMGREP_SETTINGS_FILE":        settingsFile,
		"SEMGREP_SEND_METRICS":         "off",
		"SEMGREP_ENABLE_VERSION_CHECK": "0",
		// Tracing is a SEPARATE exporter from metrics, and none of the three
		// settings above touch it. This guest has no NIC by design, so
		// semgrep's OTel exporter cannot resolve telemetry.semgrep.dev and
		// every scan burns retries on an export that can never succeed,
		// filling the serial log with resolver failures (seen 2026-09-04).
		// Scans DO complete with this noise present, so this is waste and
		// diagnostic clutter rather than a known outage cause. These are the
		// vendor-neutral SDK kill switches, so they keep working if semgrep
		// renames its own knobs.
		"OTEL_SDK_DISABLED":     "true",
		"OTEL_TRACES_EXPORTER":  "none",
		"OTEL_METRICS_EXPORTER": "none",
		"OTEL_LOGS_EXPORTER":    "none",
	} {
		if err := os.Setenv(k, v); err != nil {
			return "", err
		}
	}
	logger.Info("offline engine env set", "home", guestHome, "settings", settingsFile)
	return settingsFile, nil
}

// EnvOr returns the value of key, or def if key is unset or empty.
func EnvOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
