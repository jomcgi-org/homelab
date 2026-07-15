// Command ember-runtime-guest-init is the PID 1 of the R1 zip-lane
// runtime-python microVM (ADR embervm/002). A raw Firecracker boot ignores the
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
)

// shimCmd is the python bootstrap shim invocation, matching the apko entrypoint
// in runtimes/python/apko.yaml (kept in sync with it).
var shimCmd = []string{"/usr/bin/python3", "/usr/local/bin/ember-runtime-shim"}

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

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the
	// OCI image config), so the frozen-contract defaults the apko image bakes
	// (EMBER_HANDLER etc.) are not present. Set PATH plus the zip-lane defaults
	// explicitly here; the workload's init_env (e.g. EMBER_HANDLER from the CR's
	// handler) is layered by noded on top of the guest kernel cmdline / env, so
	// only the static defaults live here.
	setDefaultEnv(logger)

	// exec (not fork+exec): the python shim replaces this process as PID 1, so
	// there is no supervisor layer to add latency or to reap. execShim only
	// returns on failure.
	return execShim(logger)
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
