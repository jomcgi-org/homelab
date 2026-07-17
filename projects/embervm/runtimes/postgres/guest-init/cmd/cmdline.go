package main

import (
	"encoding/base64"
	"log/slog"
	"os"
	"strings"
)

// procCmdlinePath is the kernel command line the stateful boot-args ride in on.
// A var (not a const) so the test can point it at a fixture file.
var procCmdlinePath = "/proc/cmdline"

// volumeDevCmdlineKey / volumeMountCmdlineKey are the boot-arg tokens noded's
// driver.bootArgsFor appends ONLY for a STATEFUL cold boot (R4):
// `ember.volume_dev=<dev>` names the writable volume block device
// (statefulVolumeDevice in the driver, /dev/vdc), and `ember.volume_mount=<path>`
// is the guest mount path from the CR's volumeMountPath. A base build carries
// neither, which is how this init distinguishes a base build (answer vsock ready
// only) from a stateful cold boot (mount the volume, launch Postgres).
const (
	volumeDevCmdlineKey   = "ember.volume_dev"
	volumeMountCmdlineKey = "ember.volume_mount"
)

// mmdsEnvCmdlinePrefix is the boot-arg token prefix noded's driver.bootArgsFor
// appends for a STATEFUL FRESH/COLD boot's mmds_env (R4, D-R4.PR-7.1: MMDS-lite
// over boot-args). Each entry rides as `ember.env.<KEY>=<base64url(value)>`; the
// guest process env variable name is exactly <KEY>, so a workload's secretRef
// naming POSTGRES_PASSWORD gets POSTGRES_PASSWORD set directly in the
// environment the Postgres bootstrap reads. A base build and a RELIGHT carry no
// such token, so this decoder is a complete no-op for them.
const mmdsEnvCmdlinePrefix = "ember.env."

// statefulVolumeFromCmdline reads /proc/cmdline and returns the volume device
// and mount path when the stateful boot-args are present, or ("", "") for a base
// build (no volume boot-arg). A missing /proc/cmdline (host build) also returns
// ("", ""), so a host build behaves like a base build (ready-only, no Postgres).
func statefulVolumeFromCmdline(logger *slog.Logger) (dev, mountPath string) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return "", ""
	}
	dev = valueFromCmdline(string(raw), volumeDevCmdlineKey)
	if dev == "" {
		return "", ""
	}
	mountPath = valueFromCmdline(string(raw), volumeMountCmdlineKey)
	if mountPath == "" {
		// A volume device with no mount path is a malformed stateful boot; fall
		// back to the CR default so the datastore still has a home. The driver
		// always emits both together, so this is defensive.
		mountPath = "/data"
		logger.Warn("ember.volume_dev present but ember.volume_mount unset; defaulting", "mount", mountPath)
	}
	return dev, mountPath
}

// setMmdsEnv reads /proc/cmdline for every `ember.env.<KEY>=<base64url>` token
// (R4, D-R4.PR-7.1) and sets each decoded value as a process env var named
// exactly <KEY>. A missing /proc/cmdline, no matching tokens, an invalid KEY, or
// a value that fails base64url decoding are each skipped individually (never
// fatal): one malformed secret must not block every other one, and a Postgres
// bootstrap that genuinely needs POSTGRES_PASSWORD will fail its own readiness
// loudly downstream. Duplicate keys: the last occurrence wins.
func setMmdsEnv(logger *slog.Logger) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return
	}
	values := mmdsEnvFromCmdline(string(raw))
	if len(values) == 0 {
		return
	}
	keys := make([]string, 0, len(values))
	for k, v := range values {
		if err := os.Setenv(k, v); err != nil {
			// Log only the KEY name, never the value: mmds_env carries the
			// Postgres password, and this failure path must not persist it in
			// plaintext logs any more than the success path does.
			logger.Warn("could not set mmds env var", "key", k, "err", err)
			continue
		}
		keys = append(keys, k)
	}
	logger.Info("stateful first-boot env: set from mmds_env boot-args", "keys", keys)
}

// mmdsEnvFromCmdline extracts every `ember.env.<KEY>=<base64url>` token from a
// kernel command line and returns the decoded KEY -> value map. A KEY failing
// isValidEnvKeyName or a value failing base64url decode is skipped. Uses
// base64.RawURLEncoding (no padding, URL-safe alphabet), matching exactly what
// the noded driver's mmdsEnvBootArgs encodes with and what the python runtime
// guest-init decodes with.
func mmdsEnvFromCmdline(cmdline string) map[string]string {
	out := map[string]string{}
	for _, tok := range strings.Fields(cmdline) {
		k, val, ok := strings.Cut(tok, "=")
		if !ok || val == "" || !strings.HasPrefix(k, mmdsEnvCmdlinePrefix) {
			continue
		}
		key := strings.TrimPrefix(k, mmdsEnvCmdlinePrefix)
		if !isValidEnvKeyName(key) {
			continue
		}
		decoded, err := base64.RawURLEncoding.DecodeString(val)
		if err != nil {
			continue
		}
		out[key] = string(decoded)
	}
	return out
}

// isValidEnvKeyName mirrors the noded driver's isValidMmdsEnvKey and the python
// runtime guest-init's copy exactly (a shell/kernel-cmdline-safe identifier:
// letters, digits, underscore). Duplicated per binary rather than shared for the
// same reason the python runtime copy is: no existing cross-binary shared package.
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

// valueFromCmdline extracts the value of a `key=value` token from a kernel
// command line (space-separated tokens). Returns "" when the token is absent or
// has an empty value. The last occurrence wins, matching the kernel.
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

// setDefaultEnv sets PATH, HOME, and the Postgres data/config defaults so a raw
// boot with no env still resolves initdb/postgres/psql. Mirrors the apko image
// `environment` block (which a Firecracker boot never consumes). Existing values
// (e.g. an mmds_env-provided one) are never overwritten.
func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH": "/usr/bin:/bin:/usr/local/bin:/usr/libexec/postgresql",
		"HOME": "/var/lib/postgresql",
		"LANG": "C",
	}
	for k, v := range defaults {
		if _, set := os.LookupEnv(k); set {
			continue
		}
		if err := os.Setenv(k, v); err != nil {
			logger.Warn("could not set default env", "key", k, "err", err)
		}
	}
}
