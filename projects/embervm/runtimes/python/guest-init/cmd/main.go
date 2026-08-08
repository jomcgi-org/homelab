// Command ember-runtime-guest-init is the PID 1 of the R1 zip-lane
// runtime-python microVM (ADR embervm/001). A raw Firecracker boot ignores the
// OCI image config entirely and boots init=<HarnessInit> (see the noded
// driver's bootArgs), so the apko entrypoint that runs the python bootstrap
// shim is never honoured on a Firecracker boot. This init is that missing PID
// 1: it mounts a tmpfs over /tmp (the rootfs is read-only and shared across
// every microVM restored from the warm-base snapshot, and the shim unpacks the
// archive into /tmp/ember-app), then execs the python bootstrap shim, which
// unpacks the archive, imports the handler, and serves the frozen
// HTTP-over-vsock guest contract.
//
// It is deliberately minimal: unlike the sandbox guest-init it does NOT serve a
// Go shim protocol or pre-import libraries. The python shim owns all of that;
// this init only provides a writable /tmp and hands PID 1 over to python via
// exec (so python becomes PID 1 and reaps nothing beyond itself, which is fine
// for a single-process guest).
package main

import (
	"encoding/base64"
	"fmt"
	"log/slog"
	"os"
	"strings"
)

// shimCmd is the python bootstrap shim invocation, matching the apko entrypoint
// in runtimes/python/apko.yaml (kept in sync with it).
var shimCmd = []string{"/usr/bin/python3", "/usr/local/bin/ember-runtime-shim"}

// procCmdlinePath is the kernel command line the serving-port signal rides in on.
// A var (not a const) so the test can point it at a fixture file.
var procCmdlinePath = "/proc/cmdline"

// servingPortCmdlineKey is the kernel boot-arg token noded's driver.bootArgsFor
// appends ONLY for a serving-class cold boot (`ember.serving_port=<port>`). The
// python shim reads EMBER_SERVING_PORT (set from this token below) to bind TCP on
// the tap NIC instead of vsock (D-R3.11.1). A task/session boot carries no such
// token, so EMBER_SERVING_PORT stays unset and the shim's vsock path is unchanged.
const servingPortCmdlineKey = "ember.serving_port"

// servingPortEnv is the env var the python shim reads (SERVING_PORT_ENV in shim.py).
const servingPortEnv = "EMBER_SERVING_PORT"

// handlerDiskCmdlineKey / handlerZipBytesCmdlineKey are the boot-arg tokens noded's
// driver.bootArgsFor appends ONLY for a zip-lane SERVING cold boot with a handler
// artifact drive (D-R3.11.2): `ember.handler_disk=<dev>` names the block device the
// handler zip is on (drive 2, /dev/vdb), and `ember.handler_zip_bytes=<N>` is the
// EXACT zip length. The shim reads EMBER_HANDLER_ZIP / EMBER_HANDLER_ZIP_BYTES (set
// from these below) to import the handler off the device BEFORE serving, reading only
// N bytes so it ignores the block device's sector padding (the EOCD-padding defence).
// A task/session boot, a serving relight, or an image-lane serving boot carries no
// such token, so these env vars stay unset and the shim's behavior is unchanged.
const (
	handlerDiskCmdlineKey     = "ember.handler_disk"
	handlerZipBytesCmdlineKey = "ember.handler_zip_bytes"
)

// handlerZipEnv / handlerZipBytesEnv are the env vars the python shim reads
// (HANDLER_ZIP_ENV / HANDLER_ZIP_BYTES_ENV in shim.py).
const (
	handlerZipEnv      = "EMBER_HANDLER_ZIP"
	handlerZipBytesEnv = "EMBER_HANDLER_ZIP_BYTES"
)

// volumeDevCmdlineKey / volumeMountCmdlineKey are the boot-arg tokens noded's
// driver.bootArgsFor appends ONLY for a STATEFUL cold boot (R4):
// `ember.volume_dev=<dev>` names the writable volume block device
// (statefulVolumeDevice in the driver, /dev/vdc), and `ember.volume_mount=<path>`
// is the guest mount path from the CR's volumeMountPath, threaded verbatim (its
// content is opaque to noded and to this init). A task/session/serving boot
// carries neither token, so the volume-mount step below is a complete no-op for
// them.
const (
	volumeDevCmdlineKey   = "ember.volume_dev"
	volumeMountCmdlineKey = "ember.volume_mount"
)

// mmdsEnvCmdlinePrefix is the boot-arg token prefix noded's driver.bootArgsFor
// appends for a STATEFUL FRESH/COLD cold boot's mmds_env. This MMDS-lite
// boot-argument channel can migrate to a real MMDS service if needed. Each entry rides as
// `ember.env.<KEY>=<base64url(value)>`; the guest process env variable name is
// exactly <KEY> (verbatim, not further transformed), so a workload's catalog
// entry naming e.g. POSTGRES_PASSWORD as an mmds_env key gets POSTGRES_PASSWORD
// set directly in the environment the image init inherits. A RELIGHT never
// carries these tokens (the kernel does not re-init on a snapshot resume), so
// this decoder is a complete no-op past first boot.
const mmdsEnvCmdlinePrefix = "ember.env."

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-runtime-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	// tmpfs over /tmp gives the read-only rootfs a writable unpack dir
	// (/tmp/ember-app) the shim needs, kept in RAM so one read-only rootfs file
	// backs every microVM restored from the one warm-base snapshot.
	mountTmpfsTmp(logger)

	// Mount /proc so the serving cold-boot boot-arg readers below can read
	// /proc/cmdline; a raw FC boot leaves /proc unmounted. Must precede
	// setServingPortEnv / setHandlerDiskEnv, which no-op without it.
	mountProc(logger)

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the
	// OCI image config), so the frozen-contract defaults the apko image bakes
	// (EMBER_HANDLER etc.) are not present. Set PATH plus the zip-lane defaults
	// explicitly here; the workload's init_env (e.g. EMBER_HANDLER from the CR's
	// handler) is layered by noded on top of the guest kernel cmdline / env, so
	// only the static defaults live here.
	setDefaultEnv(logger)

	// Serving cold boot (R3): translate the `ember.serving_port=` kernel boot-arg
	// into EMBER_SERVING_PORT so the shim binds TCP on the tap NIC. Absent (every
	// task/session boot) leaves the env unset and the shim on its vsock path.
	setServingPortEnv(logger)

	// Zip-lane serving cold boot (R3, D-R3.11.2): translate the
	// `ember.handler_disk=` / `ember.handler_zip_bytes=` kernel boot-args into
	// EMBER_HANDLER_ZIP / EMBER_HANDLER_ZIP_BYTES so the shim imports the handler
	// off the second drive before serving. Absent (task/session boots, relights,
	// image-lane serving) leaves the env unset and the shim's behavior unchanged.
	setHandlerDiskEnv(logger)

	// Stateful first-boot secrets (R4, D-R4.PR-7.1: MMDS-lite over boot-args):
	// translate every `ember.env.<KEY>=<base64url>` kernel boot-arg into a
	// process env var, set BEFORE mountStatefulVolume/execShim so the guest
	// process (e.g. Postgres bootstrap) sees them from its very first read of
	// the environment. Absent (every task/session/serving boot, and a stateful
	// RELIGHT) leaves the env unchanged.
	setMmdsEnv(logger)

	// Stateful volume (R4): mount the workload's writable volume BEFORE handing
	// off to the shim, so the guest process only ever sees an already-mounted
	// filesystem at its declared path. This is done unconditionally here (not
	// deferred to the shim) because the host NEVER mounts the volume: guest-init
	// is the one place in the whole system that formats-if-blank and mounts it,
	// and a volume that fails to mount must fail the boot loudly rather than let
	// a stateful guest (e.g. Postgres) start against a missing data directory.
	if err := mountStatefulVolume(logger); err != nil {
		return fmt.Errorf("mount stateful volume: %w", err)
	}

	// exec (not fork+exec): the python shim replaces this process as PID 1, so
	// there is no supervisor layer to add latency or to reap. execShim only
	// returns on failure.
	return execShim(logger)
}

// mountStatefulVolume reads /proc/cmdline for the `ember.volume_dev=` /
// `ember.volume_mount=` tokens and, when both are present, formats the device
// with ext4 if it has no existing filesystem signature (blkid reports blank),
// creates the mount path, and mounts it. Absent tokens (every task/session/
// serving boot) make this a complete no-op, so those boot classes are
// unaffected. Returns an error (which the caller treats as fatal) on any
// mkfs/mount failure: a stateful guest that cannot reach its volume must not
// report ready, so failing the boot here is the correct fail-closed behavior
// rather than exec'ing into a shim that would silently run against an empty or
// unmounted directory.
func mountStatefulVolume(logger *slog.Logger) error {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		// No kernel cmdline available (host build / no procfs): nothing to do,
		// matching setServingPortEnv/setHandlerDiskEnv's no-op posture.
		return nil
	}
	dev := valueFromCmdline(string(raw), volumeDevCmdlineKey)
	if dev == "" {
		return nil
	}
	mountPath := valueFromCmdline(string(raw), volumeMountCmdlineKey)
	if mountPath == "" {
		return fmt.Errorf("ember.volume_dev=%s present but ember.volume_mount is unset", dev)
	}
	logger.Info("stateful volume: mounting", "device", dev, "mount", mountPath)
	return mountVolumeDevice(logger, dev, mountPath)
}

// setHandlerDiskEnv reads /proc/cmdline for the `ember.handler_disk=<dev>` and
// `ember.handler_zip_bytes=<N>` tokens and, when the disk token is present, exports
// EMBER_HANDLER_ZIP (the block device) and EMBER_HANDLER_ZIP_BYTES (the exact zip
// length) for the shim. A missing /proc/cmdline or an absent disk token is a no-op,
// so task/session/relight boots are unaffected. Values pass through verbatim; the
// shim validates them and, on a malformed byte count, fails the serving boot loudly
// (a serving guest that could not import its handler must not report ready).
func setHandlerDiskEnv(logger *slog.Logger) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		return
	}
	dev := valueFromCmdline(string(raw), handlerDiskCmdlineKey)
	if dev == "" {
		return
	}
	if err := os.Setenv(handlerZipEnv, dev); err != nil {
		logger.Warn("could not set handler zip device env", "err", err)
		return
	}
	if n := valueFromCmdline(string(raw), handlerZipBytesCmdlineKey); n != "" {
		if err := os.Setenv(handlerZipBytesEnv, n); err != nil {
			logger.Warn("could not set handler zip bytes env", "err", err)
		}
	}
	logger.Info("zip-lane serving cold boot: handler disk", "device", dev)
}

// setServingPortEnv reads /proc/cmdline for the `ember.serving_port=<port>` token
// and, when present, exports it as EMBER_SERVING_PORT for the shim. A missing
// /proc/cmdline (e.g. a non-Linux host build) or an absent token is a no-op, so
// the vsock task/session boot is unaffected. The value is passed through verbatim;
// the shim validates it (a malformed value degrades to the vsock path there).
func setServingPortEnv(logger *slog.Logger) {
	raw, err := os.ReadFile(procCmdlinePath)
	if err != nil {
		// No kernel cmdline available (host build / no procfs): nothing to do.
		return
	}
	port := servingPortFromCmdline(string(raw))
	if port == "" {
		return
	}
	if err := os.Setenv(servingPortEnv, port); err != nil {
		logger.Warn("could not set serving port env", "err", err)
		return
	}
	logger.Info("serving cold boot: TCP mode", "port", port)
}

// servingPortFromCmdline extracts the value of the `ember.serving_port=` token
// from a kernel command line (space-separated `key` / `key=value` tokens). Returns
// "" when the token is absent or has an empty value. The last occurrence wins,
// matching how the kernel treats duplicated cmdline keys.
func servingPortFromCmdline(cmdline string) string {
	return valueFromCmdline(cmdline, servingPortCmdlineKey)
}

// valueFromCmdline extracts the value of a `key=value` token from a kernel command
// line (space-separated `key` / `key=value` tokens). Returns "" when the token is
// absent or has an empty value. The last occurrence wins, matching how the kernel
// treats duplicated cmdline keys.
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

// setMmdsEnv reads /proc/cmdline for every `ember.env.<KEY>=<base64url>` token
// (R4, D-R4.PR-7.1) and sets each decoded value as a process env var named
// exactly <KEY>. A missing /proc/cmdline, no matching tokens, a KEY that fails
// isValidEnvKeyName, or a value that fails base64url decoding are each skipped
// individually (never fatal): one malformed secret must not block every other
// one from being delivered, and a stateful guest whose bootstrap genuinely
// needs the missing var will fail its own readiness loudly downstream, which is
// the correct place for that failure to surface (not here, where guest-init has
// no way to know which vars are actually required by the image). Duplicate
// keys: the last occurrence wins, matching valueFromCmdline's convention.
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
			// Log only the KEY name, never the value: mmds_env may carry a secret
			// (e.g. a Postgres password), and this failure path must not persist
			// it in plaintext logs any more than the success path does.
			logger.Warn("could not set mmds env var", "key", k, "err", err)
			continue
		}
		keys = append(keys, k)
	}
	logger.Info("stateful first-boot env: set from mmds_env boot-args", "keys", keys)
}

// mmdsEnvFromCmdline extracts every `ember.env.<KEY>=<base64url>` token from a
// kernel command line and returns the decoded KEY -> value map. A KEY failing
// isValidEnvKeyName or a value failing base64url decode is skipped (not fatal);
// this mirrors the noded driver's own key-validation posture so a malformed or
// adversarial token on either side of the seam degrades the same way. Uses
// base64.RawURLEncoding (no padding, URL-safe alphabet), matching exactly what
// the driver's mmdsEnvBootArgs encodes with.
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

// isValidEnvKeyName mirrors the noded driver's isValidMmdsEnvKey exactly (a
// shell/kernel-cmdline-safe identifier: letters, digits, underscore). Kept as
// an independent copy rather than a shared package because the two live in
// separate Go modules/binaries (noded vs guest-init) with no existing shared
// dependency between them; duplicating this small pure function is simpler and
// safer than introducing a new cross-binary shared package for one predicate.
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

// setDefaultEnv sets PATH and the baked frozen-contract defaults, so a raw boot
// with no env still has them. These mirror the apko.yaml `environment` block
// (which a Firecracker boot never consumes). The shim reads EMBER_* at boot; a
// per-registration override (EMBER_HANDLER from the CR handler) is injected by
// noded and takes precedence when present.
func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH":              "/usr/bin:/bin:/usr/local/bin",
		"HOME":              "/home/runtime",
		"PYTHONUNBUFFERED":  "1",
		"MPLBACKEND":        "Agg",
		"EMBER_HANDLER":     "app.handle",
		"EMBER_INVOKE_PATH": "/invoke",
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
