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
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
	"golang.org/x/sys/unix"
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
	// egdArg points the JVM's SecureRandom seed source at /dev/urandom. In a
	// NIC-less Firecracker microVM the kernel CRNG can take a long time to reach
	// "initialized" (little entropy is gathered without disks/network/interrupts),
	// and the bazel server JVM blocks on getrandom() during SecureRandom init,
	// which presents as a warming VM stuck at ZERO CPU with no bazel output. Seeding
	// from /dev/urandom (non-blocking) removes that stall. Passed as a SECOND
	// --host_jvm_args startup option (bazel accepts the flag repeatedly, each adding
	// one JVM arg), kept inside buildArgv so warming and serving stay identical.
	egdArg       = "--host_jvm_args=-Djava.security.egd=file:/dev/urandom"
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

	// Bring up the loopback interface and set the hostname BEFORE warming. The
	// bazel client talks to the bazel SERVER over a gRPC socket on 127.0.0.1; a
	// raw Firecracker boot leaves `lo` DOWN, so that connect blocks forever and
	// the warming VM sits at zero CPU. Setting the hostname (with 127.0.0.1
	// localhost + the hostname baked into /etc/hosts) also stops the JVM stalling
	// on an InetAddress.getLocalHost() reverse lookup. Both are best-effort: a
	// failure is logged loudly to the console but does not abort the boot (so the
	// failure is diagnosable rather than a silent exit), and if warming then still
	// hangs the console output pinpoints which hedge did not take.
	bringUpLoopback(logger)
	setHostname(logger)

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
	// spawned worker threads quiesce) before the snapshot is cut. A per-second tick
	// loop (instead of one time.After(settleDelay)) is instrumentation: if the
	// ticks never print, in-guest timers are broken; if they print and later lines
	// vanish, the pause/console theory revives. Every branch logs so the settle
	// window can never be a silent gap.
	logger.Info("ember-bazel-init: settling before ready", "settle", settleDelay.String())
	for i := 1; i <= int(settleDelay.Seconds()); i++ {
		select {
		case <-time.After(time.Second):
			logger.Info("ember-bazel-init: settle tick", "i", i)
		case <-ctx.Done():
			logger.Warn("ember-bazel-init: settle interrupted by signal")
			return waitForShutdown(ctx, serveErr, logger)
		}
	}
	logWarmMemoryMetrics(logger)
	// Log the flip on BOTH sides of ready.Store, so a snapshot pause landing
	// between the store and a single log line cannot hide the transition.
	logger.Info("ember-bazel-init: ready flipping")
	ready.Store(true)
	logger.Info("ember-bazel-init: ready flipped, warm base ready")

	// Hold PID 1. Exiting PID 1 panics the guest kernel before noded can snapshot
	// the base or before a restored clone can answer its one query.
	return waitForShutdown(ctx, serveErr, logger)
}

// logWarmMemoryMetrics is temporary instrumentation for GitHub issue #4054.
// This runs after warming and settling, immediately before ready is flipped:
// Firecracker captures the guest RAM at that instant when it cuts the warm
// base snapshot. In particular, tmpfs pages are unevictable, so /tmp usage is
// a hard floor for memMib. Measurements are best-effort and must never fail
// the base build.
func logWarmMemoryMetrics(logger *slog.Logger) {
	attrs := []any{"phase", "measurement for #4054"}

	var st unix.Statfs_t
	if err := unix.Statfs("/tmp", &st); err != nil {
		logger.Warn("ember-bazel-init: tmpfs memory measurement failed", "err", err)
		errValue := fmt.Sprintf("error: %v", err)
		attrs = append(attrs,
			"tmpfs_mib_total", errValue,
			"tmpfs_mib_used", errValue,
			"tmpfs_mib_free", errValue,
		)
	} else {
		blockSize := uint64(st.Bsize)
		total := st.Blocks * blockSize / (1024 * 1024)
		free := st.Bfree * blockSize / (1024 * 1024)
		used := total - free
		attrs = append(attrs,
			"tmpfs_mib_total", total,
			"tmpfs_mib_used", used,
			"tmpfs_mib_free", free,
		)
	}

	data, err := os.ReadFile("/proc/meminfo")
	if err == nil {
		var fields map[string]uint64
		fields, err = parseMeminfoFields(string(data))
		if err == nil {
			attrs = append(attrs,
				"mem_total_mib", fields["MemTotal"],
				"mem_free_mib", fields["MemFree"],
				"mem_available_mib", fields["MemAvailable"],
				"mem_cached_mib", fields["Cached"],
			)
		}
	}
	if err != nil {
		logger.Warn("ember-bazel-init: /proc/meminfo measurement failed", "err", err)
		errValue := fmt.Sprintf("error: %v", err)
		attrs = append(attrs,
			"mem_total_mib", errValue,
			"mem_free_mib", errValue,
			"mem_available_mib", errValue,
			"mem_cached_mib", errValue,
		)
	}
	logger.Log(context.Background(), slog.LevelInfo, "ember-bazel-init: warm memory metrics", attrs...)
}

// parseMeminfoFields extracts the requested /proc/meminfo values and converts
// its kB values to MiB. Requiring all fields keeps a partial measurement from
// looking complete in the structured snapshot instrumentation.
func parseMeminfoFields(input string) (map[string]uint64, error) {
	want := map[string]bool{
		"MemTotal": true, "MemFree": true, "MemAvailable": true, "Cached": true,
	}
	fields := make(map[string]uint64, len(want))
	for _, line := range strings.Split(input, "\n") {
		parts := strings.Fields(line)
		if len(parts) < 2 || !want[strings.TrimSuffix(parts[0], ":")] {
			continue
		}
		name := strings.TrimSuffix(parts[0], ":")
		value, err := strconv.ParseUint(parts[1], 10, 64)
		if err != nil || len(parts) != 3 || parts[2] != "kB" {
			return nil, fmt.Errorf("invalid %s line %q", name, line)
		}
		fields[name] = value / 1024
	}
	for name := range want {
		if _, ok := fields[name]; !ok {
			return nil, fmt.Errorf("missing %s", name)
		}
	}
	return fields, nil
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
		egdArg,
		// --max_idle_secs=0 disables the bazel server's idle-shutdown timer. The
		// warm server IS the base snapshot; a restored clone resumes it possibly
		// long after the snapshot was cut, and if the guest clock is corrected
		// FORWARD on restore (POST /shim/clock after a session relight) the server
		// could see itself as long-idle and self-exit before serving the query,
		// silently discarding the warm analysis graph. 0 means never idle-exit.
		"--max_idle_secs=0",
		"cquery", expr,
		"--noenable_bzlmod",
		"--distdir=" + distDir,
		"--experimental_convenience_symlinks=ignore",
		"--output=label",
	}
}

// warm runs the warming cquery and returns an error if it does not exit 0. It
// PIPES the bazel subprocess stdout and stderr straight through to PID 1's own
// stdout/stderr (the guest console, which noded captures), so the warming phase
// is visible in noded logs in real time; without this a stall (e.g. the JVM
// blocking on entropy, or the client blocking connecting to the server over
// loopback) is a blind zero-CPU hang with no output. Stderr is ALSO teed into an
// in-memory capture so the "Analyzed ... (N packages loaded ...)" line can still
// be grepped afterwards (the baseline; a restored clone re-emitting "0 packages
// loaded" is the proof the snapshot reused this analysis). Wall-time checkpoints
// (client started / exited) bracket the run so a hang's phase is obvious. No
// timeout here: warming an image on a cold CI runner is legitimately minutes;
// BuildBase's own BootReadyTimeout is the outer bound.
func warm(ctx context.Context, logger *slog.Logger) error {
	argv := buildArgv(warmExpr)
	logger.Info("ember-bazel-init: warming client starting", "expr", warmExpr, "argv", argv)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...) // nosemgrep: no-shell-command-injection
	cmd.Dir = workspaceDir
	cmd.Env = append(os.Environ(), "HOME="+homeDir)

	// Stdout streams to the console only (bazel writes the label list there; it is
	// large and not needed for the Analyzed grep). Stderr streams to the console
	// AND a capture builder for the Analyzed line. Capturing stderr in memory is
	// fine: bazel's stderr is progress lines, kilobytes, not the big label list.
	var stderrCap strings.Builder
	cmd.Stdout = os.Stdout
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderrCap)

	t0 := time.Now()
	// Start (not Run) so the client PID is logged the moment it launches: if the
	// process never even starts, that is a different failure than a mid-run stall.
	if err := cmd.Start(); err != nil {
		logger.Error("ember-bazel-init: warming client failed to start", "err", err)
		return err
	}
	logger.Info("ember-bazel-init: bazel client started", "pid", cmd.Process.Pid)
	err := cmd.Wait()
	elapsed := time.Since(t0)
	if err != nil {
		logger.Error("ember-bazel-init: warming client exited non-zero", "err", err, "elapsed", elapsed.String(), "tail", tail([]byte(stderrCap.String()), stderrTail))
		return err
	}
	logger.Info("ember-bazel-init: warming client exited 0", "elapsed", elapsed.String())

	// Warming succeeded (exit 0), but a flag typo can make cquery a silent no-op
	// (e.g. an unrecognized target pattern that matches nothing, or a flag that
	// changes --output so the "Analyzed" progress line never appears). If bazel
	// analyzed nothing, the snapshot would capture a server with an EMPTY graph
	// and every clone would then re-analyze from cold. Surface that here, at base
	// build, instead of only discovering it at first query. Note: at WARMING time
	// the line legitimately reports NON-zero packages loaded (this IS the cold
	// analysis); the "0 packages loaded" invariant applies to restored CLONES,
	// checked in handleQuery, not here.
	analyzed := analyzedLineFromStderr(stderrCap.String())
	if analyzed == "" {
		logger.Warn("ember-bazel-init: warming exit 0 but no 'Analyzed' line found; the snapshot may capture an EMPTY analysis graph (check for a flag typo)", "tail", tail([]byte(stderrCap.String()), stderrTail))
	} else {
		logger.Info("ember-bazel-init: warming output", "analyzed", analyzed)
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

// queryError is the JSON body returned for a FAILED query (bad expression,
// bazel non-zero exit, or in-guest timeout). It is returned with HTTP 200, not a
// non-2xx: EmberVM's task pipeline relays only a SUCCESSFUL-task guest response
// verbatim to the caller; a guest non-2xx is treated as a failed task and
// replaced with the pipeline's own dead-letter envelope, so bazel's real error
// text would never reach the backend or the visitor. A visitor's typo'd query is
// a successful DEMO run whose payload happens to carry the failure, hence 200.
// It carries NO labels/analyzed_line, so the edge discriminates success from
// failure on the presence of the "error" key.
type queryError struct {
	Error    string `json:"error"`
	ExitCode int    `json:"exit_code"`
	WallMs   int64  `json:"wall_ms"`
}

// queryErrorBody marshals a queryError. Split out so the failure-payload shape is
// unit-tested and every failure path (validation, non-zero exit, timeout) emits
// an identical envelope.
func queryErrorBody(errText string, exitCode int, wallMs int64) []byte {
	body, _ := json.Marshal(queryError{Error: errText, ExitCode: exitCode, WallMs: wallMs})
	return body
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
	// Log the FIRST /shim/ready poll received and the FIRST answered 200 (once
	// each, never per-poll: noded's WaitReady polls continuously and that would
	// spam). This shows whether WaitReady is reaching the guest at all, and the
	// moment it first observes readiness (which is when BuildBase cuts the snapshot).
	var firstPoll, firstReady sync.Once
	mux.HandleFunc("GET /shim/ready", func(w http.ResponseWriter, _ *http.Request) {
		firstPoll.Do(func() {
			logger.Info("ember-bazel-init: first /shim/ready poll received")
		})
		if ready() {
			firstReady.Do(func() {
				logger.Info("ember-bazel-init: first /shim/ready answered 200")
			})
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
// these and is then destroyed by Assign.
//
// EVERY visitor-facing failure (a malformed body, a validation rejection, a bazel
// non-zero exit from a typo'd query, or the in-guest timeout) is returned as HTTP
// 200 with a queryError payload, NOT a non-2xx. The reason is the EmberVM task
// pipeline: it relays a successful-task guest response verbatim, but replaces a
// guest non-2xx with its own dead-letter envelope, so bazel's real error text
// would never reach the backend. A bad visitor query is a SUCCESSFUL demo run
// whose payload carries the failure. The edge (bazel_core.run_query) turns an
// error-key payload back into a 422 for the browser.
func handleQuery(w http.ResponseWriter, r *http.Request, logger *slog.Logger) {
	var req queryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusOK, queryErrorBody("malformed request body: "+err.Error(), -1, 0))
		return
	}
	if err := validateExpr(req.Expression); err != nil {
		writeJSON(w, http.StatusOK, queryErrorBody("invalid expression: "+err.Error(), -1, 0))
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
		writeJSON(w, http.StatusOK, queryErrorBody("query exceeded "+queryTimeout.String()+" time budget", -1, wallMs))
		return
	}
	if err != nil {
		// A visitor's bad query (unknown target, syntax error) exits non-zero;
		// surface bazel's stderr tail so they see the real error. Still HTTP 200
		// (a successful demo run) so the pipeline relays it instead of dead-lettering.
		exitCode := -1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		}
		writeJSON(w, http.StatusOK, queryErrorBody(tail([]byte(stderr.String()), stderrTail), exitCode, wallMs))
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
	writeJSON(w, http.StatusOK, body)
}

// writeJSON writes a JSON body with an explicit Content-Length so the response is
// fixed-length framed, not chunked: the vsock transport surfaced chunked bodies
// as malformed-encoding resets on the daemon side (see shim.Server.invokeHandler
// for the same defence).
func writeJSON(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
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
