// Command ember-bazel-init is the PID 1 of the bazel-query demo microVM (ADR
// embervm/010). A raw Firecracker boot ignores the OCI image config entirely and
// boots init=<HarnessInit> (see the noded driver's bootArgs), so the apko
// entrypoint is never honoured; this init is that missing PID 1.
//
// Unlike the other embervm runtimes there is only ONE boot class here: a plain
// cold boot with no volume and no mmds_env. The whole point of this guest is the
// warm-base snapshot, so the boot does the warming inline and the snapshot
// captures a live, warm bazel SERVER:
//
//  1. Mount /proc and a large tmpfs at /tmp. Bazel's install base and output
//     base MUST be on tmpfs: the base-snapshot rootfs is read-only and shared by
//     every restored clone (noded RootfsReadOnly), so all mutable bazel state has
//     to live in RAM, where the memfile captures it.
//  2. Run the warming `bazel cquery //absl/...` (buildArgv). This starts the JVM
//     bazel server, loads + analyzes the Abseil graph into the server's heap, and
//     the CLIENT then exits.
//  3. Only AFTER the warming client has exited cleanly (ADR condition 1), wait a
//     short settle delay and flip ready. noded's BuildBase WaitReady sees
//     GET /shim/ready return 200 and cuts the base snapshot with the warm server
//     still resident. If warming FAILS we never flip ready, so the base build
//     fails loudly on BootReadyTimeout rather than snapshotting a cold server.
//  4. Serve the vsock guest contract forever. Restored clones resume inside this
//     serve loop; each serves exactly one POST /query (task-class Assign destroys
//     the VM after the response, so one query per clone is the reap).
//
// buildArgv is the single source of truth for the warming argv AND every serving
// argv, so serving flags can never drift from warming flags (ADR condition 2:
// any delta silently discards the analysis cache and a restored clone re-analyzes
// from cold, which the "0 packages loaded" proof line would then betray).
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// validateExpr rejection reasons, kept as sentinel errors so the tests assert on
// behavior (accept/reject) rather than message text.
var (
	errEmptyExpr     = errors.New("expression is empty")
	errExprTooLong   = errors.New("expression exceeds 512 bytes")
	errExprMultiline = errors.New("expression must be a single line")
	errExprCharset   = errors.New("expression contains a disallowed character")
	errExprFlagToken = errors.New("expression contains a token starting with '-'")
)

const (
	workspaceDir = "/opt/abseil"  // Abseil checkout (read-only rootfs); cquery's Dir
	outputRoot   = "/tmp/bazel"   // tmpfs: install base + output base, captured by the memfile
	homeDir      = "/tmp/home"    // tmpfs HOME; bazel writes ~/.cache and a lock dir under it
	distDir      = "/opt/distdir" // vendored dep archives (read-only rootfs); offline --distdir
	warmExpr     = "//absl/..."   // the warming query: analyze the whole Abseil graph
	heapArg      = "--host_jvm_args=-Xmx1g"
	queryTimeout = 15 * time.Second // per-serving-query wall budget (guest side)
	settleDelay  = 10 * time.Second // post-warming idle before ready, lets the JVM GC settle
	maxOutput    = 256 << 10        // bytes; cap the labels payload over vsock
	stderrTail   = 2 << 10          // bytes of bazel stderr surfaced to the visitor on error

	// queryPath is the guest HTTP path an Assign POSTs to. It MUST match the
	// workload's invokePath (chart values) exactly, else noded's round-trip 404s.
	queryPath = "/query"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-bazel-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A raw FC boot hands PID 1 no environment. HOME must point at tmpfs (bazel
	// writes a lock + cache under it and the read-only rootfs has no writable
	// home), and PATH must resolve the cc toolchain probe's compiler.
	mountProc(logger)
	mountTmpfsTmp(logger)
	if err := os.MkdirAll(homeDir, 0o755); err != nil {
		logger.Warn("mkdir HOME failed", "dir", homeDir, "err", err)
	}
	setDefaultEnv(logger)

	// ready gates GET /shim/ready. It stays false until the warming client has
	// exited cleanly and the settle delay has passed, so a base build never
	// snapshots a cold-or-broken server (a warming failure leaves ready false and
	// the base build times out loudly).
	var ready atomic.Bool

	// Bring up the vsock server first so BuildBase's WaitReady can always reach
	// /shim/ready, and so restored clones (which resume mid-serve) already have a
	// live listener. Off Linux / no vsock this degrades to a closed channel.
	serveErr := startVsockServer(ctx, logger, ready.Load)

	// Warm the server inline. buildArgv(warmExpr) is the exact same argv shape
	// every serving query uses.
	if err := warm(ctx, logger); err != nil {
		// Do NOT flip ready: the base build must fail on BootReadyTimeout rather
		// than snapshot a server whose analysis cache is empty or broken.
		logger.Error("warming cquery failed; NOT flipping ready (base build will time out)", "err", err)
		return waitForShutdown(ctx, serveErr, logger)
	}

	// The warming client has exited. Let the JVM settle (idle GC, any lazily
	// spawned worker threads quiesce) before the snapshot is cut.
	select {
	case <-time.After(settleDelay):
	case <-ctx.Done():
		return waitForShutdown(ctx, serveErr, logger)
	}
	ready.Store(true)
	logger.Info("ember-bazel-init: warm base ready", "settle", settleDelay.String())

	// Hold PID 1. Exiting PID 1 panics the guest kernel before noded can snapshot
	// the base or before a restored clone can answer its one query.
	return waitForShutdown(ctx, serveErr, logger)
}

// buildArgv is the SINGLE source of truth for every bazel invocation, warming and
// serving alike (ADR embervm/010 condition 2). The startup options
// (--output_user_root, --host_jvm_args) precede the `cquery` command; the
// expression is EXACTLY one argv element, so it can never be split into extra
// flags or a second command. A golden test freezes this slice so any flag edit
// is loud in review.
func buildArgv(expr string) []string {
	return []string{
		"/usr/local/bin/bazel",
		"--output_user_root=" + outputRoot,
		heapArg,
		"cquery", expr,
		"--noenable_bzlmod",
		"--distdir=" + distDir,
		"--experimental_convenience_symlinks=ignore",
		"--output=label",
	}
}

// warm runs the warming cquery and returns an error if it does not exit 0. Its
// stderr is logged (the "Analyzed ... (N packages loaded ...)" line is the
// baseline; a restored clone re-emitting "0 packages loaded" is the proof the
// snapshot reused this analysis). No timeout here: warming an image on a cold CI
// runner is legitimately minutes; BuildBase's own BootReadyTimeout is the outer
// bound.
func warm(ctx context.Context, logger *slog.Logger) error {
	argv := buildArgv(warmExpr)
	logger.Info("ember-bazel-init: warming", "expr", warmExpr, "argv", argv)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) // nosemgrep: no-shell-command-injection
	cmd.Dir = workspaceDir
	cmd.Env = append(os.Environ(), "HOME="+homeDir)
	out, err := cmd.CombinedOutput()
	logger.Info("ember-bazel-init: warming output", "analyzed", analyzedLineFromStderr(string(out)))
	if err != nil {
		logger.Error("ember-bazel-init: warming exited non-zero", "err", err, "tail", tail(out, stderrTail))
		return err
	}
	return nil
}

// queryResult is the JSON body returned by POST /query. analyzed_line is the
// ADR's proof-of-restore + drift detector: on a warm clone it reads
// "Analyzed N targets (0 packages loaded, 0 targets configured)".
type queryResult struct {
	Labels       string `json:"labels"`
	Truncated    bool   `json:"truncated"`
	AnalyzedLine string `json:"analyzed_line"`
	WallMs       int64  `json:"wall_ms"`
}

// queryRequest is the POST /query body.
type queryRequest struct {
	Expression string `json:"expression"`
}

// newMux builds the guest HTTP surface: the frozen shim readiness contract plus
// the one POST /query invoke path. It is a standalone mux (not shim.NewServer)
// because the shim server hardwires /invoke, and this workload's invokePath is
// /query. The readiness paths mirror the shim contract byte-for-byte so
// BuildBase's WaitReady behaves identically.
func newMux(ready func() bool, logger *slog.Logger) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /shim/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /shim/ready", func(w http.ResponseWriter, _ *http.Request) {
		if ready() {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	})
	mux.HandleFunc("POST "+queryPath, func(w http.ResponseWriter, r *http.Request) {
		handleQuery(w, r, logger)
	})
	return mux
}

// handleQuery serves one visitor cquery. A restored clone serves exactly one of
// these and is then destroyed by Assign. A bad expression (validation failure or
// a bazel non-zero exit, which a visitor's typo'd query is the normal cause of)
// returns 422 carrying bazel's real error text, so the demo surfaces the actual
// query error rather than a generic 5xx.
func handleQuery(w http.ResponseWriter, r *http.Request, logger *slog.Logger) {
	var req queryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "malformed request body: "+err.Error())
		return
	}
	if err := validateExpr(req.Expression); err != nil {
		writeErr(w, http.StatusUnprocessableEntity, "invalid expression: "+err.Error())
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), queryTimeout)
	defer cancel()

	argv := buildArgv(req.Expression)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) // nosemgrep: no-shell-command-injection
	cmd.Dir = workspaceDir
	cmd.Env = append(os.Environ(), "HOME="+homeDir)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	t0 := time.Now()
	err := cmd.Run()
	wallMs := time.Since(t0).Milliseconds()

	if ctx.Err() == context.DeadlineExceeded {
		writeErr(w, http.StatusUnprocessableEntity, "query exceeded "+queryTimeout.String()+" time budget")
		return
	}
	if err != nil {
		// A visitor's bad query (unknown target, syntax error) exits non-zero;
		// surface bazel's stderr tail so they see the real error, as a 422 (the
		// input was the problem, not the server).
		writeErr(w, http.StatusUnprocessableEntity, tail([]byte(stderr.String()), stderrTail))
		return
	}

	labels, truncated := truncate([]byte(stdout.String()), maxOutput)
	analyzed := analyzedLineFromStderr(stderr.String())
	if !strings.Contains(analyzed, "0 packages loaded") {
		// Drift alarm: a warm clone must reuse the snapshot's Skyframe graph. Non
		// -zero packages loaded means flags drifted or the snapshot was cold.
		logger.Warn("ember-bazel-init: analyzed line lacks '0 packages loaded' (possible drift/cold)", "analyzed", analyzed)
	}
	body, _ := json.Marshal(queryResult{
		Labels:       labels,
		Truncated:    truncated,
		AnalyzedLine: analyzed,
		WallMs:       wallMs,
	})
	// Explicit Content-Length so the response is fixed-length framed, not chunked:
	// the vsock transport surfaced chunked bodies as malformed-encoding resets on
	// the daemon side (see shim.Server.invokeHandler for the same defence).
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

// writeErr writes a plain-text error body with an explicit Content-Length (same
// fixed-length-framing reason as handleQuery's success path).
func writeErr(w http.ResponseWriter, status int, msg string) {
	b := []byte(msg)
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.WriteHeader(status)
	_, _ = w.Write(b)
}

// validateExpr is the guest's independent gate on the visitor expression (ADR
// embervm/010 Security; the ember_public edge validates first, this is defense in
// depth). Rules: non-empty, single line, at most 512 bytes, every character in a
// query-syntax allow-list, and no whitespace-delimited token beginning with `-`
// (that would reach bazel as an option, e.g. --output=starlark, which is code
// execution). The expression is passed as one argv element regardless, so this
// is belt-and-braces over the argv-shape guarantee.
func validateExpr(expr string) error {
	if strings.TrimSpace(expr) == "" {
		return errEmptyExpr
	}
	if len(expr) > 512 {
		return errExprTooLong
	}
	if strings.ContainsAny(expr, "\n\r") {
		return errExprMultiline
	}
	for _, r := range expr {
		if !isAllowedExprRune(r) {
			return errExprCharset
		}
	}
	for _, tok := range strings.Fields(expr) {
		if strings.HasPrefix(tok, "-") {
			return errExprFlagToken
		}
	}
	return nil
}

// isAllowedExprRune is the query-expression charset: label/target characters
// [A-Za-z0-9_/:.@~+*-], the query-function punctuation ()"', comma and =, and
// space. Anything else (shell metacharacters, braces, backticks, dollars, pipes,
// semicolons) is rejected.
func isAllowedExprRune(r rune) bool {
	switch {
	case r >= 'A' && r <= 'Z':
		return true
	case r >= 'a' && r <= 'z':
		return true
	case r >= '0' && r <= '9':
		return true
	}
	return strings.ContainsRune("_/:.@~+*-()\"', = ", r)
}

// analyzedLineFromStderr extracts bazel's "Analyzed N targets (...)" progress
// line from stderr (with or without an "INFO: " prefix). It is the proof-of
// -restore line: a warm clone reports "(0 packages loaded, 0 targets
// configured)". Returns "" when absent (the caller logs a drift warning).
func analyzedLineFromStderr(stderr string) string {
	sc := bufio.NewScanner(strings.NewReader(stderr))
	sc.Buffer(make([]byte, 0, 64<<10), 1<<20)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		line = strings.TrimPrefix(line, "INFO: ")
		if strings.HasPrefix(line, "Analyzed ") {
			return line
		}
	}
	return ""
}

// truncate caps b at max bytes, returning the (possibly shortened) string and
// whether it was cut. Keeps the labels payload bounded over the vsock transport.
func truncate(b []byte, max int) (string, bool) {
	if len(b) <= max {
		return string(b), false
	}
	return string(b[:max]), true
}

// tail returns the last n bytes of b as a string (the useful end of a bazel
// error is its final lines, so surface the tail rather than the head).
func tail(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[len(b)-n:])
}

// setDefaultEnv sets PATH + HOME for a raw FC boot (which inherits no
// environment). PATH must include /usr/bin (gcc, the cc toolchain probe) and
// /usr/local/bin (the vendored bazel). Mirrors the apko environment block, which
// a Firecracker boot never consumes.
func setDefaultEnv(logger *slog.Logger) {
	defaults := map[string]string{
		"PATH": "/usr/local/bin:/usr/bin:/bin",
		"HOME": homeDir,
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

// waitForShutdown blocks until the context is cancelled (SIGTERM) or the vsock
// server errors, returning nil on a clean shutdown so run's error contract holds.
func waitForShutdown(ctx context.Context, serveErr <-chan error, logger *slog.Logger) error {
	select {
	case <-ctx.Done():
		logger.Info("ember-bazel-init: shutdown signal")
		return nil
	case err := <-serveErr:
		if err != nil {
			logger.Warn("ember-bazel-init: vsock server stopped", "err", err)
		}
		return nil
	}
}

// guestHTTPPort is the frozen guest-contract vsock port (1027) BuildBase's
// WaitReady dials and Assign delivers /query over. Aliased for readability.
const guestHTTPPort = vsockproto.GuestHTTPPort
