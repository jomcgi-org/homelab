// Package fullscan runs a whole-tree `semgrep scan --pro` (interfile) over a
// batch of in-memory files, materialized into a tmpfs tmpdir. Unlike the warm
// mcp scan-server (single-file), this path sees the whole file set on disk at
// once, so cross-file dataflow fires.
package fullscan

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/cliout"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// Runner executes the scan over a materialized tree dir and returns raw
// `semgrep --json` cli_output bytes. It is a seam so tests avoid the real
// engine.
type Runner func(ctx context.Context, treeDir string) ([]byte, error)

// Scan writes req.Files into a fresh tmpdir tree, runs runner over it, parses
// the cli_output (rewriting tmpdir-absolute result paths back to
// repo-relative via the tmpdir prefix), and cleans up the tmpdir. A file
// whose cleaned path would escape the tree (e.g. "../x") is rejected with an
// error.
func Scan(ctx context.Context, req vsockproto.ScanRequest, runner Runner) (vsockproto.ScanResult, error) {
	dir, err := os.MkdirTemp("/tmp", "sgfull-")
	if err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("fullscan: create tree dir: %w", err)
	}
	defer os.RemoveAll(dir)

	for _, f := range req.Files {
		clean := filepath.Clean(string(os.PathSeparator) + f.Path)
		dst := filepath.Join(dir, clean)
		if !strings.HasPrefix(dst, dir+string(os.PathSeparator)) {
			return vsockproto.ScanResult{}, fmt.Errorf("fullscan: file path %q escapes tree dir", f.Path)
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return vsockproto.ScanResult{}, fmt.Errorf("fullscan: mkdir for %q: %w", f.Path, err)
		}
		if err := os.WriteFile(dst, []byte(f.Content), 0o644); err != nil {
			return vsockproto.ScanResult{}, fmt.Errorf("fullscan: write %q: %w", f.Path, err)
		}
	}

	out, runErr := runner(ctx, dir)
	if runErr != nil && len(out) == 0 {
		return vsockproto.ScanResult{}, fmt.Errorf("fullscan: run scan: %w", runErr)
	}

	res, err := cliout.Parse(out, dir)
	if err != nil {
		return vsockproto.ScanResult{}, err
	}

	// semgrep exits non-zero WITH cli_output when it finds issues (or hits a
	// partial rule/parse error), so a runErr alongside output is not a hard
	// failure: surface it alongside the findings we did parse.
	if runErr != nil {
		res.Errors = append(res.Errors, runErr.Error())
	}

	return res, nil
}

// SemgrepRunner runs the python `semgrep scan --pro` CLI over treeDir and
// returns stdout (cli_output JSON). rulesDir is the --config path
// (SEMGREP_SCAN_RULES). The exact flags/env that make the CLI locate the
// proprietary core offline are verified post-deploy in a later task; adjust
// from that finding.
func SemgrepRunner(rulesDir string) Runner {
	return func(ctx context.Context, treeDir string) ([]byte, error) {
		// Invoke the baked offline-Pro engine (osemgrep-pro, the OCaml CLI) DIRECTLY
		// rather than the python `semgrep scan --pro`. pysemgrep looks for the pro
		// core at its own bundled path and, not finding it, tries to DOWNLOAD it
		// from semgrep.dev (semgrep install-semgrep-pro), which fails in the
		// egress-less guest (DNS failure -> exit 2). pysemgrep is also a different
		// version (1.165) than the baked engine (1.161), so satisfying its
		// version-stamp check would drive a mismatched core. osemgrep-pro is the
		// same self-contained binary the warm mcp scan-server uses: it has a `scan`
		// subcommand and the interfile engine built in, so `osemgrep-pro scan --pro`
		// does whole-tree interfile analysis offline with no download and no
		// pysemgrep coupling. Offline Pro unlock + metrics/version-check-off come
		// from the env guestboot.SetupEnv set (SEMGREP_SETTINGS_FILE, HOME,
		// SEMGREP_SEND_METRICS=off, SEMGREP_ENABLE_VERSION_CHECK=0).
		bin := os.Getenv("OSEMGREP_PRO_BIN")
		if bin == "" {
			bin = "/opt/semgrep/osemgrep-pro"
		}
		cmd := exec.CommandContext(ctx, bin, "scan", "--pro",
			"--config", rulesDir, "--metrics=off", "--json", treeDir)
		cmd.Stderr = os.Stderr
		return cmd.Output()
	}
}
