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

// volumeDevCmdlineKey / volumeMountCmdlineKey are the stateful-lane boot-arg
// tokens (R4): `ember.volume_dev=<dev>` names the writable volume block device
// (statefulVolumeDevice in the driver, /dev/vdc), `ember.volume_mount=<path>` the
// guest mount path from the CR's volumeMountPath. A base build carries neither,
// which is how this init distinguishes a base build (answer vsock ready only)
// from a stateful cold boot (mount the volume, run k3s). Mirrors the postgres
// runtime guest-init exactly.
const (
	volumeDevCmdlineKey   = "ember.volume_dev"
	volumeMountCmdlineKey = "ember.volume_mount"
)

// statefulVolumeFromCmdline reads /proc/cmdline and returns the volume device and
// mount path when the stateful boot-args are present, or ("", "") for a base
// build (no volume boot-arg). A missing /proc/cmdline (host build) also returns
// ("", ""), so a host build behaves like a base build. Mirrors the postgres
// runtime guest-init.
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
		// back to the k3s data dir so the datastore still has a home. The driver
		// always emits both together, so this is defensive.
		mountPath = k3sDataDir
		logger.Warn("ember.volume_dev present but ember.volume_mount unset; defaulting", "mount", mountPath)
	}
	return dev, mountPath
}

// k3sDataDir is k3s's writable state root (server sqlite db, kubelet, containerd
// state). The stateful lane's volume mounts here so the datastore is durable; a
// composite member (warmth-only, no volume) runs k3s here on the ephemeral
// writable rootfs subtree (mountGuestFilesystems makes it writable), losing state
// on fresh/destroy by design. It is also the CR's volumeMountPath in the drill.
const k3sDataDir = "/var/lib/rancher/k3s"

// bootClass is the guest boot lane this VM was started in. ONE k3s rootfs serves
// three: a base build (snapshot bake, no k3s), a stateful cold boot (postgres
// precedent, volume-backed k3s), and an R5 composite member (warmth-only,
// volume-less k3s). Deciding it purely from the volume boot-arg was correct until
// R5: a composite member is a real k3s boot that carries NO volume (a standing R5
// warmth-only decision), so volume-presence alone misclassifies every member as a
// base build and k3s never runs. classifyBoot adds the composite lane.
type bootClass int

const (
	// bootBaseBuild: no volume and no composite facts. Answer the vsock ready
	// contract only; run no k3s. noded's BuildBase snapshots then reaps the VM.
	bootBaseBuild bootClass = iota
	// bootStateful: a writable volume boot-arg is present (the scratch-postgres
	// lane). Mount the volume at k3s's data dir, then run k3s.
	bootStateful
	// bootComposite: an R5 composite group member (EMBER_GROUP_MEMBER injected),
	// warmth-only with no volume. Run k3s directly on the ephemeral writable rootfs
	// subtree; there is no volume to mount.
	bootComposite
)

// classifyBoot decides the boot lane from the resolved volume device (dev, "" when
// absent) and an env lookup (env, os.Getenv in production, after setMmdsEnv has
// decoded the boot-args). A volume is unambiguously the stateful lane. Absent a
// volume, an injected EMBER_GROUP_MEMBER fact marks a composite member that MUST
// still run k3s; only a boot with neither signal is a base build. Pure over its
// inputs so it is table-testable without a microVM.
func classifyBoot(dev string, env func(string) string) bootClass {
	if dev != "" {
		return bootStateful
	}
	if env(memberEnv) != "" {
		return bootComposite
	}
	return bootBaseBuild
}

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
