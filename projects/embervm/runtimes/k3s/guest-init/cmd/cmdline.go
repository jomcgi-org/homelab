package main

import (
	"encoding/base64"
	"log/slog"
	"os"
	"strings"
)

// procCmdlinePath is the kernel command line the boot-args ride in on. A var
// (not a const) so the test can point it at a fixture file.
var procCmdlinePath = "/proc/cmdline"

// mmdsEnvCmdlinePrefix is the MMDS-lite boot-arg token prefix (D-R4.PR-7.1). Each
// entry rides as `ember.env.<KEY>=<base64url(value)>`; the guest process env var
// name is exactly <KEY>. For the composite (R5) guest the platform injects the
// generic EMBER_GROUP_* facts (standing decision 13) through this seam; for the
// Task 3 spike they arrive via the serving lane's mmds_env, identically decoded.
const mmdsEnvCmdlinePrefix = "ember.env."

// servingPortCmdlineKey is the serving-lane health-port boot-arg (R3). Present
// only for a serving-class cold boot; the spike boots the k3s-server as a
// serving-class workload so it arrives, but k3s binds its own fixed ports, so
// EMBER_SERVING_PORT is informational here.
const servingPortCmdlineKey = "ember.serving_port"

// servingPortEnv is the env var name the serving-port boot-arg maps to.
const servingPortEnv = "EMBER_SERVING_PORT"

// setMmdsEnv decodes every `ember.env.<KEY>=<base64url>` boot-arg into a process
// env var named exactly <KEY>. A missing /proc/cmdline, no matching tokens, an
// invalid KEY, or a value that fails base64url decode are each skipped
// individually (never fatal): one malformed fact must not block the others, and
// a k3s bootstrap that genuinely needs a missing fact (e.g. EMBER_GROUP_SECRET)
// fails its own readiness loudly downstream. Duplicate keys: last occurrence
// wins. Mirrors the postgres/python runtime guest-init decoders exactly (the
// same wire, base64.RawURLEncoding, isValidEnvKeyName predicate).
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
			// Log only the KEY name, never the value: EMBER_GROUP_SECRET is a
			// secret, and this failure path must not persist it in plaintext.
			logger.Warn("could not set mmds env var", "key", k, "err", err)
			continue
		}
		keys = append(keys, k)
	}
	logger.Info("composite first-boot env: set from mmds_env boot-args", "keys", keys)
}

// mmdsEnvFromCmdline extracts every `ember.env.<KEY>=<base64url>` token and
// returns the decoded KEY -> value map. A KEY failing isValidEnvKeyName or a
// value failing base64url decode is skipped (not fatal). Uses
// base64.RawURLEncoding (no padding, URL-safe), matching the noded driver's
// encoder and the other runtime guest-init decoders.
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

// setServingPortEnv reads `ember.serving_port=<port>` into EMBER_SERVING_PORT. A
// missing /proc/cmdline or absent token is a no-op.
func setServingPortEnv(logger *slog.Logger) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return
	}
	port := valueFromCmdline(string(raw), servingPortCmdlineKey)
	if port == "" {
		return
	}
	if err := os.Setenv(servingPortEnv, port); err != nil {
		logger.Warn("could not set serving port env", "err", err)
		return
	}
	logger.Info("serving cold boot: health-port signal", "port", port)
}

// valueFromCmdline extracts the value of a `key=value` token from a kernel
// command line (space-separated tokens). Returns "" when the token is absent or
// empty. The last occurrence wins, matching the kernel.
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

// isValidEnvKeyName mirrors the noded driver's isValidMmdsEnvKey and the other
// runtime guest-init copies exactly (a shell/kernel-cmdline-safe identifier:
// letters, digits, underscore). Duplicated per binary for the same reason the
// python/postgres copies are: no existing cross-binary shared package.
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

// setDefaultEnv sets PATH plus the k3s defaults a raw boot lacks (the apko
// `environment` block a Firecracker boot never consumes). Existing values (an
// mmds_env-provided one) are never overwritten. k3s and its embedded tools
// (containerd, runc, the CNI plugins, iptables) live under /usr/local/bin and
// the Wolfi /usr/{s}bin, and KUBECONFIG points at the server's admin config so
// an in-guest `k3s kubectl` works for the drill.
func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH":       "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME":       "/root",
		"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml",
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
