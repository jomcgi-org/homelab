package main

import (
	"log/slog"
	"os"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/bootenv"
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

// statefulVolumeFromCmdline reads /proc/cmdline and returns the volume device
// and mount path when the stateful boot-args are present, or ("", "") for a base
// build (no volume boot-arg). A missing /proc/cmdline (host build) also returns
// ("", ""), so a host build behaves like a base build (ready-only, no Postgres).
func statefulVolumeFromCmdline(logger *slog.Logger) (dev, mountPath string) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return "", ""
	}
	dev = bootenv.Value(string(raw), volumeDevCmdlineKey)
	if dev == "" {
		return "", ""
	}
	mountPath = bootenv.Value(string(raw), volumeMountCmdlineKey)
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
	values := bootenv.Decode(string(raw))
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
