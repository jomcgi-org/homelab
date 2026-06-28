// Command fc-agent-init is the in-microVM wrapper from ADR 022: the guest's
// PID 1. It launches the agent harness, watches for a quiescent idle boundary
// and signals the controller over vsock that it is safe to snapshot, and on a
// wake (after restore) re-establishes the guest's connections before handing
// control back to the harness.
//
// Phase 2 wires the components (idle detector, reconnect manager, vsock
// protocol). The host transport is a vsock connection in production; the dial is
// kept behind a small seam so the same wiring is exercised over a pipe in tests.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/harness"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/idle"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/reconnect"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agent-init/internal/vsockdial"
	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("fc-agent-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the
	// OCI image config), so establish the baseline the harness needs to find its
	// tools. goose lives in /usr/local/bin; gh/git/node in /usr/bin. HOME anchors
	// goose's config under /home/goose-agent (matching the harness image).
	ensureEnv("PATH", "/usr/local/bin:/usr/bin:/bin:/sbin:/usr/local/sbin")
	ensureEnv("HOME", "/home/goose-agent")
	// The harness image stores recipes here; goose's default search path does not
	// include it, so point goose at it explicitly.
	ensureEnv("GOOSE_RECIPE_PATH", "/home/goose-agent/recipes")

	// Raw FC boot with no init system leaves the loopback interface DOWN. Bring it
	// up before anything uses 127.0.0.1: the transparent egress funnel and the
	// wildcard DNS responder are loopback-only, so without this all guest egress
	// (the harness reaching the model) fails with "could not connect".
	bringUpLoopback(logger)

	threadID := os.Getenv("FC_THREAD_ID")
	idleAfter := durationEnv("FC_IDLE_AFTER", 60*time.Second)

	logger.Info("fc-agent-init starting", "thread_id", threadID, "idle_after", idleAfter.String())

	// Idle detector: the harness reports call boundaries via a local control
	// path (Phase 3 wiring); here we construct it and run the sampler.
	det := &idle.Detector{IdleAfter: idleAfter}

	// Reconnect manager: in production these re-open the model/MCP/git clients.
	// Registration of the concrete reconnectors is Phase 5 (it depends on the
	// harness image's client config); the manager + ordering live here.
	rec := &reconnect.Manager{Attempts: 5, Backoff: 200 * time.Millisecond}

	// Connect to the controller over vsock. On cluster this is the guest's only
	// channel to the host; off-cluster (no host listener) it returns nil and we
	// fall back to env/argv so the binary still exercises cleanly.
	conn := dialController(ctx, logger)
	if conn != nil {
		defer conn.Close()
	}

	// Determine the work. A raw FC boot gives PID 1 no env, so the task arrives
	// over vsock (Hello -> Assign). Fall back to env/argv when there is no
	// controller (tests, a warm-base probe with no task yet).
	harnessArgv := assignedHarness(logger, conn, threadID)
	if harnessArgv == nil {
		harnessArgv = harnessCommand()
	}

	// Make the guest a transparent egress funnel (ADR 023). The guest is
	// vsock-only and goose's client ignores HTTP_PROXY, so instead of any proxy
	// config we (1) answer every DNS query with 127.0.0.1 and (2) listen on the
	// egress ports on loopback, tunnelling each accepted connection over vsock to
	// the host sidecar, which routes it by SNI/Host. Set up after the injected env
	// is applied and before the harness starts resolving. Only with a controller
	// (the vsock egress hop needs the host).
	if conn != nil {
		installGuestCA(logger)
		setupTransparentEgress(ctx, logger, os.Getenv("EGRESS_PORTS"))
	}

	var harnessProc *exec.Cmd
	if len(harnessArgv) > 0 {
		harnessProc = exec.CommandContext(ctx, harnessArgv[0], harnessArgv[1:]...)
		harnessProc.Stdout = os.Stdout
		harnessProc.Stderr = os.Stderr
		harnessProc.Env = os.Environ()
		if err := harnessProc.Start(); err != nil {
			return err
		}
		logger.Info("harness started", "argv", harnessArgv)
	}

	if conn != nil {
		go serveControl(ctx, logger, conn, rec, threadID)
		go det.Run(ctx, time.Second, func(wake vsockproto.WakeCondition) {
			logger.Info("idle boundary reached; signalling controller", "wake", string(wake))
			if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindIdle, ThreadID: threadID, Wake: wake}); err != nil {
				logger.Warn("failed to send idle signal", "err", err)
			}
		})
	}

	// Block until shutdown or the harness exits.
	if harnessProc != nil {
		err := harnessProc.Wait()
		logger.Info("harness exited", "err", err)
		// Publish the artifact the harness produced (ADR 024). The monolith
		// mediates the S3 write, so the guest just POSTs the file it built; doing
		// it here (not in the goose recipe) makes publishing deterministic and
		// observable (this log is captured host-side by fc-agentd, unlike the
		// guest console which the PID-1-exit kernel panic truncates) and yields the
		// URL for the Done result. No-op when ARTIFACT_PUBLISH_URL is unset.
		dumpGuestDiag(logger)
		artifactURL := publishArtifact(logger)
		if conn != nil {
			status := "ok"
			if err != nil {
				status = err.Error()
			}
			if serr := conn.Send(vsockproto.Message{Kind: vsockproto.KindDone, ThreadID: threadID, Status: status, Result: artifactURL}); serr != nil {
				logger.Warn("failed to send done signal", "err", serr)
			}
		}
		return nil
	}
	<-ctx.Done()
	return nil
}

// assignedHarness performs the control-channel handshake: announce Hello, wait
// for the controller's Assign, and build the harness command from it. Returns
// nil if there is no controller or no usable assignment, so the caller falls
// back to env/argv.
func assignedHarness(logger *slog.Logger, conn *vsockproto.Conn, threadID string) []string {
	if conn == nil {
		return nil
	}
	if err := conn.Send(vsockproto.Message{Kind: vsockproto.KindHello, ThreadID: threadID}); err != nil {
		logger.Warn("failed to announce hello", "err", err)
		return nil
	}
	// Blocking read: the controller replies with Assign immediately after Hello.
	// This is the only reader until serveControl starts, so there is no race.
	msg, err := conn.Recv()
	if err != nil {
		logger.Warn("failed to receive task assignment", "err", err)
		return nil
	}
	if msg.Kind != vsockproto.KindAssign {
		logger.Warn("expected task assignment, got other message", "kind", string(msg.Kind))
		return nil
	}
	// Apply the controller-injected harness env (goose provider/model + the
	// in-cluster model base URL): cluster config the guest cannot hardcode.
	for k, v := range msg.Env {
		_ = os.Setenv(k, v)
	}
	logger.Info("task assignment received", "recipe", msg.Recipe, "env_keys", len(msg.Env))
	return harness.GooseCommand(harness.Config{Recipe: msg.Recipe, Task: msg.Task})
}

// serveControl handles host->guest messages: on a wake, reconnect then ack.
func serveControl(ctx context.Context, logger *slog.Logger, conn *vsockproto.Conn, rec *reconnect.Manager, threadID string) {
	for {
		msg, err := conn.Recv()
		if err != nil {
			return
		}
		switch msg.Kind {
		case vsockproto.KindWake:
			logger.Info("wake received; reconnecting", "wake", string(msg.Wake))
			if rerr := rec.Reconnect(ctx); rerr != nil {
				logger.Error("reconnect failed after wake", "err", rerr)
				continue
			}
			if serr := conn.Send(vsockproto.Message{Kind: vsockproto.KindResumeAck, ThreadID: threadID}); serr != nil {
				logger.Warn("failed to send resume ack", "err", serr)
			}
		case vsockproto.KindHeartbeat:
			_ = conn.Send(vsockproto.Message{Kind: vsockproto.KindHeartbeat, ThreadID: threadID})
		default:
			logger.Debug("ignoring message", "kind", string(msg.Kind))
		}
	}
}

func harnessCommand() []string {
	for i, a := range os.Args {
		if a == "--" {
			return os.Args[i+1:]
		}
	}
	// Goose mode (Plan B): a turn-capped recipe run is the agent harness. The
	// recipe's own max_turns bounds it and the between-turns boundary is what the
	// idle detector snapshots on.
	if g := harness.GooseCommand(harness.Config{
		Recipe: os.Getenv("FC_GOOSE_RECIPE"),
		Task:   os.Getenv("FC_TASK"),
	}); g != nil {
		return g
	}
	if cmd := os.Getenv("FC_HARNESS_CMD"); cmd != "" {
		return []string{"/bin/sh", "-c", cmd}
	}
	return nil
}

// dialController opens the control channel to the host over vsock (host CID 2,
// ControlPort). It retries briefly: the guest can boot to this point before the
// controller has its per-thread listener ready, so connection-refused is normal
// for the first attempts. It returns nil when no controller answers (off-cluster
// or no vsock device), and the caller then runs from env/argv with no channel.
func dialController(ctx context.Context, logger *slog.Logger) *vsockproto.Conn {
	deadline := time.Now().Add(5 * time.Second)
	for {
		rwc, err := vsockdial.Dial(vsockproto.HostCID, vsockproto.ControlPort)
		if err == nil {
			logger.Info("controller control channel connected", "port", vsockproto.ControlPort)
			return vsockproto.NewConn(rwc)
		}
		if time.Now().After(deadline) {
			logger.Info("no controller on vsock; idle/reconnect run locally", "err", err)
			return nil
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// defaultEgressPorts are the loopback ports the funnel captures when EGRESS_PORTS
// is unset: HTTP, HTTPS, and the in-cluster model's port. With wildcard DNS every
// name resolves to loopback, so a destination on any other port connects to
// loopback with no listener and fails closed rather than escaping. Generic
// any-port capture (iptables REDIRECT) is future work (ADR 023).
var defaultEgressPorts = []int{80, 443, 8080}

// guestCABundle is the system trust bundle most TLS clients (incl. Go's gh) read.
const guestCABundle = "/etc/ssl/certs/ca-certificates.crt"

// installGuestCA installs the egress CA into the guest trust store (ADR 023 6b).
// The CA cert (public PEM) arrives in EGRESS_CA_CERT over the trusted vsock Assign
// channel; the harness must trust it so it accepts the leaf certs the egress-proxy
// mints when it TLS-terminates a secret-bearing destination. No-op when unset
// (6a, or a thread with no secrets). Runs before the harness so clients pick it up
// at startup.
func installGuestCA(logger *slog.Logger) {
	caPEM := os.Getenv("EGRESS_CA_CERT")
	if strings.TrimSpace(caPEM) == "" {
		return
	}
	if f, err := os.OpenFile(guestCABundle, os.O_APPEND|os.O_WRONLY, 0o644); err != nil {
		logger.Warn("guest CA: open trust bundle failed", "err", err)
	} else {
		_, werr := f.WriteString("\n" + caPEM + "\n")
		_ = f.Close()
		if werr != nil {
			logger.Warn("guest CA: append to trust bundle failed", "err", werr)
		}
	}
	// A standalone file + explicit env vars cover tools that do not read the bundle
	// (node adds NODE_EXTRA_CA_CERTS to its defaults rather than replacing them).
	const caFile = "/etc/ssl/certs/egress-ca.pem"
	if err := os.WriteFile(caFile, []byte(caPEM), 0o644); err != nil {
		logger.Warn("guest CA: write standalone cert failed", "err", err)
	}
	ensureEnv("SSL_CERT_FILE", guestCABundle)
	ensureEnv("GIT_SSL_CAINFO", guestCABundle)
	ensureEnv("NODE_EXTRA_CA_CERTS", caFile)
	logger.Info("guest egress CA installed in trust store")
}

// setupTransparentEgress makes the guest a dumb egress funnel (ADR 023). It
// points the resolver at a wildcard responder that answers every name with
// 127.0.0.1, then listens on each egress port on loopback and tunnels every
// accepted connection over vsock to the host sidecar. The guest decides nothing
// about routing; the sidecar recovers the real destination from the SNI/Host.
func setupTransparentEgress(ctx context.Context, logger *slog.Logger, portsSpec string) {
	writeResolvConf(logger)
	go runWildcardDNS(ctx, logger)
	for _, port := range parseEgressPorts(portsSpec) {
		go runFunnel(ctx, logger, port)
	}
}

// writeResolvConf points the guest's stub resolver at the in-guest wildcard DNS.
func writeResolvConf(logger *slog.Logger) {
	if err := os.WriteFile("/etc/resolv.conf", []byte("nameserver 127.0.0.1\n"), 0o644); err != nil {
		logger.Warn("write /etc/resolv.conf failed; harness name resolution may fail", "err", err)
	}
}

// parseEgressPorts parses a comma-separated port list, falling back to
// defaultEgressPorts when empty or fully invalid.
func parseEgressPorts(spec string) []int {
	var ports []int
	for _, p := range strings.Split(spec, ",") {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		n, err := strconv.Atoi(p)
		if err != nil || n <= 0 || n > 65535 {
			continue
		}
		ports = append(ports, n)
	}
	if len(ports) == 0 {
		return defaultEgressPorts
	}
	return ports
}

// runWildcardDNS answers every DNS query on 127.0.0.1:53 with an A record of
// 127.0.0.1 (and NODATA for non-A types), so every hostname the harness resolves
// points at the loopback funnel. It returns when ctx is done.
func runWildcardDNS(ctx context.Context, logger *slog.Logger) {
	pc, err := net.ListenPacket("udp", "127.0.0.1:53")
	if err != nil {
		logger.Warn("wildcard DNS listen failed; harness name resolution will fail", "err", err)
		return
	}
	go func() {
		<-ctx.Done()
		_ = pc.Close()
	}()
	buf := make([]byte, 512)
	for {
		n, addr, err := pc.ReadFrom(buf)
		if err != nil {
			if ctx.Err() == nil {
				logger.Warn("wildcard DNS read", "err", err)
			}
			return
		}
		if resp := buildDNSResponse(buf[:n]); resp != nil {
			if _, err := pc.WriteTo(resp, addr); err != nil {
				logger.Warn("wildcard DNS write", "err", err)
			}
		}
	}
}

// buildDNSResponse builds a minimal reply to a single-question DNS query: an A
// query is answered with 127.0.0.1; any other type gets NODATA (NOERROR, no
// answer) so the resolver falls back to the A record. Returns nil on a malformed
// query.
func buildDNSResponse(req []byte) []byte {
	if len(req) < 12 {
		return nil
	}
	// Walk the question's QNAME to find QTYPE/QCLASS (4 bytes after the root label).
	off := 12
	for off < len(req) {
		l := int(req[off])
		if l == 0 {
			off++
			break
		}
		if l&0xc0 != 0 { // a compression pointer has no place in a query QNAME
			return nil
		}
		off += l + 1
	}
	if off+4 > len(req) {
		return nil
	}
	qtype := uint16(req[off])<<8 | uint16(req[off+1])
	qend := off + 4
	isA := qtype == 1

	resp := make([]byte, 0, qend+16)
	resp = append(resp, req[0], req[1]) // echo ID
	resp = append(resp, 0x81, 0x80)     // QR=1, RD=1, RA=1, RCODE=0
	resp = append(resp, 0x00, 0x01)     // QDCOUNT=1
	if isA {
		resp = append(resp, 0x00, 0x01) // ANCOUNT=1
	} else {
		resp = append(resp, 0x00, 0x00) // ANCOUNT=0 (NODATA)
	}
	resp = append(resp, 0x00, 0x00, 0x00, 0x00) // NSCOUNT=0, ARCOUNT=0
	resp = append(resp, req[12:qend]...)        // echo the question
	if isA {
		resp = append(resp,
			0xc0, 0x0c, // NAME: pointer to the question name at offset 12
			0x00, 0x01, // TYPE A
			0x00, 0x01, // CLASS IN
			0x00, 0x00, 0x00, 0x3c, // TTL 60s
			0x00, 0x04, // RDLENGTH 4
			0x7f, 0x00, 0x00, 0x01, // 127.0.0.1
		)
	}
	return resp
}

// runFunnel listens on loopback:port and tunnels each accepted connection to the
// host sidecar over vsock. It returns when ctx is done.
func runFunnel(ctx context.Context, logger *slog.Logger, port int) {
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		logger.Warn("egress funnel listen failed", "addr", addr, "err", err)
		return
	}
	logger.Info("egress funnel listening", "addr", addr)
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		local, err := ln.Accept()
		if err != nil {
			if ctx.Err() == nil {
				logger.Warn("egress funnel accept", "addr", addr, "err", err)
			}
			return
		}
		go funnelToHost(logger, local, port)
	}
}

// funnelToHost dials the host over vsock, writes a one-line preamble carrying the
// original destination port (the listener's port), then copies bytes both ways.
// The sidecar recovers the destination host from the SNI/Host header and combines
// it with this port to reach the real upstream.
func funnelToHost(logger *slog.Logger, local net.Conn, port int) {
	defer local.Close()
	up, err := vsockdial.Dial(vsockproto.HostCID, vsockproto.EgressPort)
	if err != nil {
		logger.Warn("egress funnel vsock dial", "port", port, "err", err)
		return
	}
	defer up.Close()
	if _, err := io.WriteString(up, strconv.Itoa(port)+"\n"); err != nil {
		logger.Warn("egress funnel preamble write", "port", port, "err", err)
		return
	}
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, local); done <- struct{}{} }()
	go func() { _, _ = io.Copy(local, up); done <- struct{}{} }()
	<-done
}

// artifactFile is where the artifact recipe writes the page to publish.
const artifactFile = "/tmp/artifact.html"

// dumpGuestDiag logs what the harness actually did, host-side via the controller
// (fc-agentd captures this, unlike the guest console which the PID-1-exit panic
// truncates): a listing of likely write targets (did goose write a file at all,
// and where?) and the tail of goose's newest session/log file (did it emit a
// tool call, and what did the model return?). Diagnostic only; bounded output.
func dumpGuestDiag(logger *slog.Logger) {
	home := os.Getenv("HOME")
	for _, dir := range []string{"/tmp", home, "/", "/workspace"} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		var names []string
		for _, e := range entries {
			info, _ := e.Info()
			sz := int64(-1)
			if info != nil {
				sz = info.Size()
			}
			names = append(names, e.Name()+"("+strconv.FormatInt(sz, 10)+")")
		}
		logger.Info("diag: dir", "path", dir, "entries", strings.Join(names, " "))
	}
	// Newest goose session/log file under HOME (goose stores transcripts + logs
	// there); its tail shows the model turns + whether a tool call was emitted.
	var newest string
	var newestT time.Time
	if home != "" {
		_ = filepath.WalkDir(home, func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			if strings.HasSuffix(p, ".jsonl") || strings.HasSuffix(p, ".log") {
				if info, e := d.Info(); e == nil && info.ModTime().After(newestT) {
					newestT, newest = info.ModTime(), p
				}
			}
			return nil
		})
	}
	if newest == "" {
		logger.Info("diag: no goose session/log file found under HOME")
		return
	}
	b, err := os.ReadFile(newest)
	if err != nil {
		logger.Info("diag: read goose log failed", "path", newest, "err", err)
		return
	}
	if len(b) > 16384 {
		b = b[len(b)-16384:]
	}
	logger.Info("diag: goose log tail", "path", newest, "tail", string(b))
}

// publishArtifact POSTs the artifact the harness built to the monolith (ADR 024),
// which performs the S3 write (the guest holds no S3 credential), and returns the
// public URL. It is a no-op (returns "") when ARTIFACT_PUBLISH_URL is unset (any
// non-artifact tier) or no artifact file was produced. The request egresses
// through the transparent funnel like all guest traffic; doing the publish here
// rather than in the goose recipe makes it deterministic and logs the exact
// result host-side (the guest console is truncated by the PID-1-exit panic). An
// ARTIFACT_ID re-publishes the same artifact id (hot reload across iterations).
func publishArtifact(logger *slog.Logger) string {
	url := os.Getenv("ARTIFACT_PUBLISH_URL")
	if url == "" {
		return ""
	}
	html, err := os.ReadFile(artifactFile)
	if err != nil {
		logger.Info("artifact: nothing to publish", "path", artifactFile, "err", err)
		return ""
	}
	body := map[string]string{"html": string(html)}
	if id := os.Getenv("ARTIFACT_ID"); id != "" {
		body["id"] = id
	}
	payload, err := json.Marshal(body)
	if err != nil {
		logger.Error("artifact: marshal failed", "err", err)
		return ""
	}
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Post(url, "application/json", bytes.NewReader(payload))
	if err != nil {
		logger.Error("artifact: publish request failed", "url", url, "err", err)
		return ""
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
	if resp.StatusCode != http.StatusOK {
		logger.Error("artifact: publish non-OK", "status", resp.StatusCode, "body", string(respBody))
		return ""
	}
	var out struct {
		ID, URL, Version string
	}
	_ = json.Unmarshal(respBody, &out)
	logger.Info("artifact: published", "url", out.URL, "version", out.Version, "html_bytes", len(html))
	return out.URL
}

// ensureEnv sets key to def only when it is unset, so an injected value (e.g. a
// future SandboxTemplate) still wins over the boot-time baseline.
func ensureEnv(key, def string) {
	if os.Getenv(key) == "" {
		_ = os.Setenv(key, def)
	}
}

func durationEnv(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
