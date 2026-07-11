// Package guestboot holds the shared PID-1 boot steps for the semgrep guest
// microVMs: loopback up, a tmpfs over /tmp, the process environment, and the
// offline Pro settings file. Both inits run these before serving the shim: the
// warm scan-server init (cmd, osemgrep-pro mcp --pro, single-file) and the
// full-scan init (cmd-full, semgrep scan --pro, whole-tree interfile). Keeping
// the steps here once means the two inits differ only in what they serve and in
// the tmpfs size they ask for, not in boot logic.
package guestboot

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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

// proShimDir is the tmpfs directory where SetupProEngine builds the pro-engine
// "install" layout the full-scan CLI expects.
const proShimDir = "/tmp/sgbin"

// SetupProEngine makes the baked offline-Pro engine discoverable to
// `osemgrep-pro scan --pro`. That command (like pysemgrep) refuses to run unless
// the Pro core looks INSTALLED: a semgrep-core-proprietary binary sitting next to
// semgrep-core, plus a pro-installed-by.txt version stamp recording the version
// that installed it. Our engine is baked at /opt/semgrep (read-only rootfs), not
// in that layout, so the CLI reports "Semgrep Pro is either uninstalled or out of
// date" and exits. We reconstruct the layout in a tmpfs dir on PATH: symlinks for
// semgrep-core, semgrep-core-proprietary, and osemgrep-pro (all to the baked
// binaries), so both PATH-based and argv[0]-dir-based resolution land here, plus
// the stamp. The stamp is the engine's OWN -pro_version output (the engine is the
// CLI, so there is no version drift to guess at). Returns the pro binary path to
// invoke the scan from (inside the shim dir). Prepends the shim dir to PATH.
func SetupProEngine(logger *slog.Logger) (string, error) {
	proBin := EnvOr("OSEMGREP_PRO_BIN", DefaultOsemgrepPro)
	coreBin := filepath.Join(filepath.Dir(proBin), "semgrep-core")

	if err := os.MkdirAll(proShimDir, 0o755); err != nil {
		return "", fmt.Errorf("pro shim mkdir: %w", err)
	}
	links := map[string]string{
		"semgrep-core":             coreBin,
		"semgrep-core-proprietary": proBin,
		"osemgrep-pro":             proBin,
	}
	for name, target := range links {
		dst := filepath.Join(proShimDir, name)
		_ = os.Remove(dst)
		if err := os.Symlink(target, dst); err != nil {
			return "", fmt.Errorf("pro shim symlink %s: %w", name, err)
		}
	}

	// pro-installed-by.txt records the installing version. Use the engine's own
	// -pro_version (the method pysemgrep uses to read it), so the stamp always
	// matches the baked engine exactly.
	out, err := exec.Command(proBin, "-pro_version").Output()
	if err != nil {
		return "", fmt.Errorf("pro_version: %w", err)
	}
	stamp := strings.TrimSpace(string(out))
	stampPath := filepath.Join(proShimDir, "pro-installed-by.txt")
	if err := os.WriteFile(stampPath, []byte(stamp+"\n"), 0o644); err != nil {
		return "", fmt.Errorf("pro stamp write: %w", err)
	}

	os.Setenv("PATH", proShimDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	logger.Info("pro engine install shim ready", "dir", proShimDir, "version", stamp)
	return filepath.Join(proShimDir, "osemgrep-pro"), nil
}
