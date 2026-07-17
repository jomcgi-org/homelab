package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
)

// superviseK3s builds the k3s command line from the injected env (standing
// decision 13), writes the server's static token-auth file when this member is
// the server, then runs k3s as a child, streaming its stdio to the guest console
// and blocking until it exits or the context is cancelled (SIGTERM). It is
// fork+exec, not exec-replace, because the guest control agent goroutine must
// keep running alongside k3s for a post-resume clock resync; this init is the
// supervisor.
//
// A k3s exit is returned as an error so PID 1 exits non-zero and the failure is
// visible (a k3s that dies is a dead node); a clean SIGTERM shutdown returns nil.
func superviseK3s(ctx context.Context, logger *slog.Logger) error {
	argv, err := k3sArgv(getenv)
	if err != nil {
		return fmt.Errorf("build k3s command: %w", err)
	}

	// The server exposes a static token-auth entry derived from EMBER_GROUP_SECRET
	// (decision 13). Write it before starting k3s so --token-auth-file resolves.
	if getenv(roleEnv) == roleServer {
		if err := writeServerTokenAuth(getenv(secretEnv)); err != nil {
			return fmt.Errorf("write server token-auth file: %w", err)
		}
	}

	logger.Info("starting k3s",
		"role", getenv(roleEnv),
		"own_ip", getenv(ownIPEnv),
		"peers", peerFactsForLog(os.Environ()),
		// argv[2:] omits the binary + subcommand; --token's value is NOT logged
		// (it is the secret). Log only the flag names for the same reason.
		"flags", redactArgv(argv),
	)

	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) //nolint:gosec // argv is built from the frozen decision-13 mapping, not user input
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()

	if err := cmd.Run(); err != nil {
		// A context cancellation (SIGTERM) surfaces as an exec error too; treat a
		// cancelled context as a clean shutdown.
		if ctx.Err() != nil {
			logger.Info("k3s stopped on shutdown signal")
			return nil
		}
		return fmt.Errorf("k3s exited: %w", err)
	}
	return nil
}

// writeServerTokenAuth writes the static token-auth CSV (serverTokenAuthCSV) to
// the tmpfs path the server's --token-auth-file points at. An empty secret is a
// no-op (k3sArgv already fails a server with no secret before this is reached, so
// this is defensive). 0600 because the file contains the bearer token.
func writeServerTokenAuth(secret string) error {
	csv := serverTokenAuthCSV(secret)
	if csv == "" {
		return nil
	}
	return os.WriteFile(tokenAuthPath, []byte(csv), 0o600)
}

// redactArgv returns the argv flag NAMES for logging, dropping the value of
// --token (the secret) so a startup log never carries it. A pure helper so it is
// table-testable. Non-token values (ports, IPs, backend names) are kept: they
// are not secret and are useful for debugging a failed boot.
func redactArgv(argv []string) []string {
	out := make([]string, 0, len(argv))
	for i := 0; i < len(argv); i++ {
		out = append(out, argv[i])
		if argv[i] == "--token" && i+1 < len(argv) {
			out = append(out, "<redacted>")
			i++ // skip the secret value
		}
	}
	return out
}
