// Command goosecracker-guest-init is the PID 1 of the goosecracker agent
// microVM. It brings the guest network up, stands up the transparent egress
// funnel that lets goose reach the in-cluster model, and serves the fc-invoke
// shim protocol: an HTTP server over AF_VSOCK on vsockproto.GuestHTTPPort. Each
// /invoke request carries an AgentRequest (recipe, task, model env, progress
// URL, optional git mirror/ref); the handler runs goose once against that request
// and returns the result. Readiness is signalled via GET /shim/ready so the
// fc-invoke daemon can poll before routing an invocation.
//
// This is the disposable-VM (cold-run) model: there is no snapshot/idle detector,
// no host control channel, and no session/artifact persistence. The task arrives
// in the HTTP request body, not over a vsock control handshake. Session resume,
// artifact publish, and secret-swap (guest CA install) are deferred; the handler
// and AgentRequest leave hooks for them.
package main

import (
	"bufio"
	"context"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	"github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/goosecracker/guest-init/internal/vsockdial"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim/capabilities"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("goosecracker-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Raw FC boot with no init system leaves the loopback interface DOWN. Bring it
	// up before anything uses 127.0.0.1: the transparent egress funnel and the
	// wildcard DNS responder are loopback-only, so without this all guest egress
	// (goose reaching the model) fails with "could not connect".
	bringUpLoopback(logger)

	// tmpfs over /tmp keeps the guest's mutable scratch in RAM so the rootfs can
	// stay read-only and shareable across disposable microVMs.
	mountTmpfsTmp(logger)

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the OCI
	// image config), so establish the baseline goose needs to find its tools. goose
	// lives in /usr/local/bin; gh/git/node in /usr/bin. HOME anchors goose's config
	// under /home/goose-agent (matching the image); GOOSE_RECIPE_PATH points goose
	// at the baked recipe library. ensureEnv only sets an unset key, so an injected
	// value still wins.
	ensureEnv("PATH", "/usr/local/bin:/usr/bin:/bin:/sbin:/usr/local/sbin")
	ensureEnv("HOME", "/home/goose-agent")
	ensureEnv("GOOSE_RECIPE_PATH", "/home/goose-agent/recipes")

	if err := os.MkdirAll(handler.Workspace, 0o755); err != nil {
		return err // nosemgrep: no-bare-error-return
	}

	// Bring the transparent egress funnel up BEFORE serving (ADR 023). The guest is
	// vsock-only and goose's client ignores HTTP_PROXY, so instead of any proxy
	// config we (1) answer every DNS query with 127.0.0.1 and (2) listen on the
	// egress ports on loopback, tunnelling each accepted connection over vsock to
	// the host sidecar, which routes it by SNI/Host. This must be live before an
	// /invoke run starts goose, or the model is unreachable.
	setupTransparentEgress(ctx, logger, os.Getenv("EGRESS_PORTS"))

	ln, err := vsockdial.Listen(vsockproto.GuestHTTPPort)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	// Cold boot: goose needs no warmup, so the guest is ready as soon as it serves.
	h := handler.New(&execRunner{workspace: handler.Workspace})
	srv := shim.NewServer(h, shim.WithReady(func() bool { return true }))

	// Serve in a goroutine so ctx cancellation (SIGTERM) can close the server
	// gracefully rather than blocking indefinitely on Accept.
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()

	select {
	case <-ctx.Done():
		_ = srv.Close()
		<-serveErr
		return nil
	case err := <-serveErr:
		return err // nosemgrep: no-bare-error-return
	}
}

// execRunner is the production handler.Runner. It shells goose via os/exec,
// streaming stdout/stderr lines to onLine while capturing the full output, and
// clones git mirrors via the shared capabilities.ExecGit.
type execRunner struct {
	workspace string
}

// Run launches goose (argv) in the workspace with env overlaid on the process
// environment, forwards each output line to onLine as it is produced, and returns
// the captured output. A non-nil error is goose's exit error (it ran but exited
// non-zero), which the handler surfaces as an error result rather than a
// transport failure.
func (r *execRunner) Run(ctx context.Context, argv []string, env map[string]string, onLine func(string)) (string, error) {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = r.workspace
	cmd.Env = mergeEnv(env)

	pr, pw := io.Pipe()
	cmd.Stdout = pw
	cmd.Stderr = pw

	if err := cmd.Start(); err != nil {
		_ = pw.Close()
		return "", err
	}

	var sb strings.Builder
	scanDone := make(chan struct{})
	go func() {
		defer close(scanDone)
		sc := bufio.NewScanner(pr)
		// goose can emit long lines (tool output); raise the scanner limit so a long
		// line is streamed rather than dropped with bufio.ErrTooLong.
		sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for sc.Scan() {
			line := sc.Text()
			sb.WriteString(line)
			sb.WriteByte('\n')
			if onLine != nil {
				onLine(line)
			}
		}
	}()

	waitErr := cmd.Wait()
	// Close the write end so the scanner sees EOF, then wait for it to drain.
	_ = pw.Close()
	<-scanDone
	return sb.String(), waitErr
}

// Clone clones mirror into dest and checks out ref via the shared ExecGit.
func (r *execRunner) Clone(ctx context.Context, mirror, ref, dest string) error {
	return (&capabilities.ExecGit{}).Clone(ctx, mirror, ref, dest)
}

// mergeEnv overlays the caller-supplied env (model provider/base-url/tier keys
// the guest cannot hardcode) onto the process environment goose inherits.
// Overlaid keys win over inherited ones, and duplicates are collapsed so the
// child sees exactly one value per key.
func mergeEnv(env map[string]string) []string {
	merged := make(map[string]string, len(env))
	for _, kv := range os.Environ() {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			merged[kv[:i]] = kv[i+1:]
		}
	}
	for k, v := range env {
		merged[k] = v
	}
	out := make([]string, 0, len(merged))
	for k, v := range merged {
		out = append(out, k+"="+v)
	}
	return out
}

// defaultEgressPorts are the loopback ports the funnel captures when EGRESS_PORTS
// is unset (nothing injects EGRESS_PORTS into a raw-FC guest today, so these
// defaults are what actually apply). They must cover every port the workload's
// egress allowlist declares, since wildcard DNS points every name at loopback and
// only these ports have a funnel listener; any other port fails closed. Covered:
// 80 (MCP gateway), 443 (HTTPS/open web), 8000 (monolith progress + artifact
// sink), 8080 (in-cluster model), 4318 (SigNoz OTLP, when tracing is wired).
// Generic any-port capture (iptables REDIRECT) is future work (ADR 023).
var defaultEgressPorts = []int{80, 443, 8000, 8080, 4318}

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
		logger.Warn("write /etc/resolv.conf failed; goose name resolution may fail", "err", err)
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
// 127.0.0.1 (and NODATA for non-A types), so every hostname goose resolves points
// at the loopback funnel. It returns when ctx is done.
func runWildcardDNS(ctx context.Context, logger *slog.Logger) {
	pc, err := net.ListenPacket("udp", "127.0.0.1:53")
	if err != nil {
		logger.Warn("wildcard DNS listen failed; goose name resolution will fail", "err", err)
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

// ensureEnv sets key to def only when it is unset, so an injected value still
// wins over the boot-time baseline.
func ensureEnv(key, def string) {
	if os.Getenv(key) == "" {
		_ = os.Setenv(key, def)
	}
}
