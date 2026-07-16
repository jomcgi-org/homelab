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

	// exec (not fork+exec): the python shim replaces this process as PID 1, so
	// there is no supervisor layer to add latency or to reap. execShim only
	// returns on failure.
	return execShim(logger)
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
