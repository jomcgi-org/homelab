// Command goosecracker-guest-init is the PID 1 of the goosecracker agent
// microVM. It brings the guest network up, stands up the transparent egress
// funnel that lets goose reach the in-cluster model, and serves the fc-invoke
// shim protocol: an HTTP server over AF_VSOCK on vsockproto.GuestHTTPPort. Each
// /invoke request carries an AgentRequest (recipe, task, model env, progress
// URL, optional git mirror/ref); the handler runs goose once against that request
// and returns the result. Readiness is signalled via GET /shim/ready so the
// fc-invoke daemon can poll before routing an invocation.
//
// This is the disposable-VM (cold-run) model: there is no snapshot/idle detector
// and no host control channel. The task arrives in the HTTP request body, not
// over a vsock control handshake. Durable state is orchestrator-owned and rides
// the request/response: session resume (ADR 026 Phase 2) via the SessionStore
// seam, artifact publish via AgentResult, and the egress secret-swap CA installed
// per invoke from the request env (ADR 023 6b, see installGuestCA).
package main

import (
	"bufio"
	"context"
	"errors"
	"io"
	"io/fs"
	"log/slog"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
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

	// tmpfs over /tmp AND /workspace keeps every guest path goose WRITES to in RAM,
	// so the rootfs stays read-only and shareable across disposable microVMs. The
	// baked rootfs mounts /workspace read-only; goose (via the developer extension)
	// edits files there, and git clones a mirror into it, so it must be writable.
	// /tmp additionally backs the writable HOME set below.
	mountTmpfs(logger, "/tmp", "256m")
	mountTmpfs(logger, handler.Workspace, "256m")

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores the OCI
	// image config), so establish the baseline goose needs. goose lives in
	// /usr/local/bin; gh/git/node in /usr/bin. HOME must be WRITABLE: goose creates
	// its session database and logs under HOME (~/.local/state/goose), so anchoring
	// HOME on the read-only rootfs (/home/goose-agent) panics goose with a
	// ReadOnlyFilesystem error. Point HOME at the /tmp tmpfs instead. The XDG_*
	// dirs are set EXPLICITLY (not left to derive from HOME) because goose resolves
	// its state/data/config/cache via the XDG base dirs, and an unset XDG var can
	// resolve to a bare "/.local/..." path (off HOME) that is also unwritable.
	// GOOSE_RECIPE_PATH still points at the baked recipe library on the read-only
	// rootfs, which is fine: recipes are only read. ensureEnv only sets an unset
	// key, so an injected value still wins; HOME/XDG are force-set (a read-only
	// value must never win).
	ensureEnv("PATH", "/usr/local/bin:/usr/bin:/bin:/sbin:/usr/local/sbin")
	ensureEnv("GOOSE_RECIPE_PATH", "/home/goose-agent/recipes")
	const gooseHome = "/tmp/goose-home"
	for k, v := range map[string]string{
		"HOME":            gooseHome,
		"XDG_STATE_HOME":  gooseHome + "/.local/state",
		"XDG_DATA_HOME":   gooseHome + "/.local/share",
		"XDG_CONFIG_HOME": gooseHome + "/.config",
		"XDG_CACHE_HOME":  gooseHome + "/.cache",
	} {
		_ = os.Setenv(k, v)
		if err := os.MkdirAll(v, 0o755); err != nil {
			return err // nosemgrep: no-bare-error-return
		}
	}

	if err := os.MkdirAll(handler.Workspace, 0o755); err != nil {
		return err // nosemgrep: no-bare-error-return
	}

	// Bring the transparent egress funnel up BEFORE serving (ADR 023). The guest is
	// vsock-only and goose's client ignores HTTP_PROXY, so instead of any proxy
	// config we give every DNS name a synthetic 127.0.0.0/8 address, REDIRECT all
	// outbound TCP to one loopback capture listener, recover each connection's
	// original destination via SO_ORIGINAL_DST (reverse-mapping the synthetic
	// address to its name), and tunnel it over vsock to the host sidecar with a
	// "host:port" preamble. This must be live before an /invoke run starts goose,
	// or the model is unreachable.
	setupTransparentEgress(ctx, logger)

	ln, err := vsockdial.Listen(vsockproto.GuestHTTPPort)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	// goose stores every session in one SQLite db under XDG_DATA_HOME; the session
	// store hydrates/exports it for resume (ADR 026 Phase 2).
	sessionsDB := gooseHome + "/.local/share/goose/sessions/sessions.db"

	// Cold boot: goose needs no warmup, so the guest is ready as soon as it serves.
	h := handler.New(
		&execRunner{workspace: handler.Workspace},
		handler.WithSessionStore(&execSessionStore{dbPath: sessionsDB}),
	)
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
	// Install the egress secret-swap CA (ADR 023 6b) before goose starts, so its
	// TLS to a swapped host trusts the sidecar's minted leaf. The CA cert rides in
	// the per-invoke env (EGRESS_CA_CERT), so this happens here, not at boot.
	installGuestCA(slog.Default(), env)

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

// execSessionStore is the production handler.SessionStore. It persists goose's
// single SQLite session db at dbPath (under the guest's XDG_DATA_HOME on the /tmp
// tmpfs), hydrating it before a resume and exporting it after a run.
type execSessionStore struct {
	dbPath string
}

// Hydrate writes the prior sessions.db to dbPath so `goose run --resume` finds the
// earlier conversation. It creates the parent dir and clears any stale WAL/SHM
// sidecars so a leftover journal cannot shadow the restored db (defensive; the
// tmpfs is fresh each boot).
func (s *execSessionStore) Hydrate(_ context.Context, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(s.dbPath), 0o755); err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	for _, sidecar := range []string{s.dbPath + "-wal", s.dbPath + "-shm"} {
		if err := os.Remove(sidecar); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err // nosemgrep: no-bare-error-return
		}
	}
	return os.WriteFile(s.dbPath, data, 0o644)
}

// Export returns the current sessions.db bytes, folding goose's WAL into the main
// file first so the single exported file is complete. It returns nil (not an
// error) when no db exists, e.g. a run that never created a session, so the
// caller simply persists nothing.
func (s *execSessionStore) Export(ctx context.Context) ([]byte, error) {
	if _, err := os.Stat(s.dbPath); errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	// Best-effort WAL checkpoint via the baked sqlite3 CLI: goose checkpoints on
	// exit, but TRUNCATE guarantees the -wal is folded in before we read the single
	// file. A failure (locked db, missing binary) is logged, not fatal; the main
	// file is still read and the cold fallback covers any incompleteness.
	cmd := exec.CommandContext(ctx, "sqlite3", s.dbPath, "PRAGMA wal_checkpoint(TRUNCATE);")
	if out, err := cmd.CombinedOutput(); err != nil {
		slog.Warn("session export: wal_checkpoint failed", "err", err, "out", string(out))
	}
	return os.ReadFile(s.dbPath)
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

// capturePort is the loopback port the netfilter REDIRECT sends all captured
// guest TCP egress to. The capture listener recovers each connection's original
// destination via SO_ORIGINAL_DST, so one listener replaces the old per-port
// funnel and any destination port is captured (ADR 023 generic egress).
const capturePort = 15001

// setupTransparentEgress makes the guest a dumb egress funnel (ADR 023). goose's
// client ignores HTTP_PROXY and the guest is vsock-only, so instead of proxy
// config we (1) answer every DNS name with a unique synthetic 127.0.0.0/8 address
// and remember the name<->address mapping, (2) REDIRECT all outbound TCP to a
// single loopback capture listener via netfilter, and (3) recover each
// connection's original destination with SO_ORIGINAL_DST, reverse-map the
// synthetic address back to the name (or forward a literal IP as-is), and tunnel
// it over vsock to the host sidecar with a "host:port" preamble. The sidecar
// classifies and routes it (external allow, internal deny-by-default).
func setupTransparentEgress(ctx context.Context, logger *slog.Logger) {
	writeResolvConf(logger)
	res := newSynthResolver()
	go runWildcardDNS(ctx, logger, res)
	setupCapture(ctx, logger, res)
}

// writeResolvConf points the guest's stub resolver at the in-guest wildcard DNS.
func writeResolvConf(logger *slog.Logger) {
	if err := os.WriteFile("/etc/resolv.conf", []byte("nameserver 127.0.0.1\n"), 0o644); err != nil {
		logger.Warn("write /etc/resolv.conf failed; goose name resolution may fail", "err", err)
	}
}

// synthResolver hands every DNS name a unique synthetic address in 127.0.0.0/8
// and remembers the mapping both ways. The wildcard DNS responder answers A
// queries with the synthetic address (so every name routes to the loopback
// capture listener), and the capture listener reverse-maps the SO_ORIGINAL_DST
// address back to the real name for the sidecar preamble. Recovering the name
// (not an SNI/Host sniff) is what lets plaintext protocols with no host in the
// stream, like git://, be routed.
type synthResolver struct {
	mu     sync.Mutex
	byName map[string]netip.Addr
	byIP   map[netip.Addr]string
	next   uint32
}

func newSynthResolver() *synthResolver {
	return &synthResolver{
		byName: map[string]netip.Addr{},
		byIP:   map[netip.Addr]string{},
		next:   0x7f000002, // 127.0.0.2 (skip 127.0.0.1: DNS + capture listener live there)
	}
}

// forName returns the synthetic address for name, allocating one on first use.
func (s *synthResolver) forName(name string) netip.Addr {
	name = strings.ToLower(strings.TrimSuffix(name, "."))
	s.mu.Lock()
	defer s.mu.Unlock()
	if ip, ok := s.byName[name]; ok {
		return ip
	}
	ip := s.allocLocked()
	s.byName[name] = ip
	s.byIP[ip] = name
	return ip
}

// allocLocked returns the next free 127.0.0.0/8 address, skipping 127.0.0.1 and
// wrapping if the (enormous) space is somehow exhausted.
func (s *synthResolver) allocLocked() netip.Addr {
	for {
		v := s.next
		s.next++
		if v>>24 != 127 { // ran off the end of 127/8; wrap
			s.next = 0x7f000002
			continue
		}
		if v == 0x7f000001 { // 127.0.0.1
			continue
		}
		return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
	}
}

// name reverse-maps a synthetic address to its DNS name.
func (s *synthResolver) name(ip netip.Addr) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.byIP[ip]
	return n, ok
}

// runWildcardDNS answers every DNS query on 127.0.0.1:53: an A query gets the
// name's synthetic 127.0.0.0/8 address (allocated on demand), any other type gets
// NODATA so the resolver falls back to the A record. It returns when ctx is done.
func runWildcardDNS(ctx context.Context, logger *slog.Logger, res *synthResolver) {
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
		if resp := buildDNSResponse(buf[:n], res); resp != nil {
			if _, err := pc.WriteTo(resp, addr); err != nil {
				logger.Warn("wildcard DNS write", "err", err)
			}
		}
	}
}

// buildDNSResponse builds a minimal reply to a single-question DNS query: an A
// query is answered with the name's synthetic address from res; any other type
// gets NODATA (NOERROR, no answer) so the resolver falls back to the A record.
// Returns nil on a malformed query.
func buildDNSResponse(req []byte, res *synthResolver) []byte {
	if len(req) < 12 {
		return nil
	}
	name, off := parseQName(req, 12)
	if off < 0 || off+4 > len(req) {
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
		a4 := res.forName(name).As4()
		resp = append(resp,
			0xc0, 0x0c, // NAME: pointer to the question name at offset 12
			0x00, 0x01, // TYPE A
			0x00, 0x01, // CLASS IN
			0x00, 0x00, 0x00, 0x3c, // TTL 60s
			0x00, 0x04, // RDLENGTH 4
			a4[0], a4[1], a4[2], a4[3],
		)
	}
	return resp
}

// parseQName reads a DNS QNAME starting at off and returns the dotted name plus
// the offset just past the root label. It returns off=-1 on a malformed name
// (bad length, a compression pointer, or truncation); a query QNAME never uses
// compression.
func parseQName(req []byte, off int) (string, int) {
	var sb strings.Builder
	for off < len(req) {
		l := int(req[off])
		if l == 0 {
			return sb.String(), off + 1
		}
		if l&0xc0 != 0 { // a compression pointer has no place in a query QNAME
			return "", -1
		}
		off++
		if off+l > len(req) {
			return "", -1
		}
		if sb.Len() > 0 {
			sb.WriteByte('.')
		}
		sb.Write(req[off : off+l])
		off += l
	}
	return "", -1
}

// runCaptureListener accepts the netfilter-redirected connections on the loopback
// capture port, recovers each one's original destination via SO_ORIGINAL_DST,
// maps a synthetic address back to its DNS name (a literal IP passes through),
// and tunnels the connection to the host sidecar. It returns when ctx is done.
func runCaptureListener(ctx context.Context, logger *slog.Logger, res *synthResolver) {
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(capturePort))
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		logger.Warn("egress capture listen failed", "addr", addr, "err", err)
		return
	}
	logger.Info("egress capture listening", "addr", addr)
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		local, err := ln.Accept()
		if err != nil {
			if ctx.Err() == nil {
				logger.Warn("egress capture accept", "err", err)
			}
			return
		}
		go handleCaptured(logger, local, res)
	}
}

// handleCaptured recovers the original destination of one captured connection and
// tunnels it to the host sidecar. A synthetic 127.0.0.0/8 destination is
// reverse-mapped to its DNS name; a literal IP is forwarded as-is. It closes
// local on the error paths; funnelToHost owns it once handed over.
func handleCaptured(logger *slog.Logger, local net.Conn, res *synthResolver) {
	tcp, ok := local.(*net.TCPConn)
	if !ok {
		logger.Warn("egress capture: non-TCP conn")
		_ = local.Close()
		return
	}
	dst, err := originalDst(tcp)
	if err != nil {
		logger.Warn("egress capture: SO_ORIGINAL_DST failed", "err", err)
		_ = local.Close()
		return
	}
	host := dst.Addr().String()
	if dst.Addr().Is4() && dst.Addr().As4()[0] == 127 {
		name, ok := res.name(dst.Addr())
		if !ok {
			logger.Warn("egress capture: unknown synthetic address", "addr", dst.Addr().String())
			_ = local.Close()
			return
		}
		host = name
	}
	funnelToHost(logger, local, host, int(dst.Port()))
}

// funnelToHost dials the host sidecar over vsock, writes a one-line "host:port"
// preamble carrying the recovered destination, then copies bytes both ways. The
// sidecar classifies and routes by that destination. It owns local.
func funnelToHost(logger *slog.Logger, local net.Conn, host string, port int) {
	defer local.Close()
	up, err := vsockdial.Dial(vsockproto.HostCID, vsockproto.EgressPort)
	if err != nil {
		logger.Warn("egress funnel vsock dial", "host", host, "port", port, "err", err)
		return
	}
	defer up.Close()
	preamble := net.JoinHostPort(host, strconv.Itoa(port)) + "\n"
	if _, err := io.WriteString(up, preamble); err != nil {
		logger.Warn("egress funnel preamble write", "host", host, "port", port, "err", err)
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

// guestCABundle is the baked system trust bundle most TLS clients read. It is on
// the read-only shared rootfs, so it is readable but not writable.
const guestCABundle = "/etc/ssl/certs/ca-certificates.crt"

// tmpfs paths for the guest's writable trust material (the rootfs is read-only,
// so unlike the old writable-rootfs guest we cannot append to the baked bundle;
// we copy it to /tmp and append there). /tmp is a tmpfs mounted in run().
const (
	guestCABundleRW   = "/tmp/ca-bundle.crt"
	guestCAStandalone = "/tmp/egress-ca.pem"
)

// installGuestCA installs the egress secret-swap CA (ADR 023 6b) into the guest
// trust store for a single goose run, using the CA cert carried in the invoke env
// (EGRESS_CA_CERT). It is a no-op when that env is empty (no swap configured),
// which keeps the plain-egress and test paths untouched.
//
// The shared rootfs is read-only, so it writes writable trust material to the
// /tmp tmpfs instead of appending to the baked bundle: a full bundle (the baked
// public roots plus our CA) at guestCABundleRW, and the CA alone at
// guestCAStandalone. It then points goose's TLS clients at those via SSL_CERT_FILE
// / GIT_SSL_CAINFO (full bundle, so real HTTPS to non-swapped hosts still
// validates against the public roots) and NODE_EXTRA_CA_CERTS (node appends the
// standalone cert to its defaults). Keys are only set when unset, so an explicit
// override still wins. The real destination cert is still validated by the
// sidecar when it re-originates; this only makes the guest trust the sidecar.
func installGuestCA(logger *slog.Logger, env map[string]string) {
	caPEM := strings.TrimSpace(env["EGRESS_CA_CERT"])
	if caPEM == "" {
		return
	}

	// The baked bundle is readable on the ro rootfs; tolerate its absence (tests,
	// a stripped image) by falling back to just our CA.
	baked, err := os.ReadFile(guestCABundle)
	if err != nil {
		logger.Warn("guest CA: read baked bundle failed; using CA only", "err", err)
		baked = nil
	}
	full := append(append([]byte{}, baked...), []byte("\n"+caPEM+"\n")...)
	if err := os.WriteFile(guestCABundleRW, full, 0o644); err != nil {
		logger.Warn("guest CA: write bundle failed; egress swap TLS may fail", "err", err)
		return
	}
	if err := os.WriteFile(guestCAStandalone, []byte(caPEM), 0o644); err != nil {
		logger.Warn("guest CA: write standalone cert failed", "err", err)
	}

	setIfUnset(env, "SSL_CERT_FILE", guestCABundleRW)
	setIfUnset(env, "GIT_SSL_CAINFO", guestCABundleRW)
	setIfUnset(env, "NODE_EXTRA_CA_CERTS", guestCAStandalone)
	logger.Info("guest egress CA installed", "bundle", guestCABundleRW)
}

// setIfUnset sets m[key]=val only when key is absent or empty, so an explicit
// value in the invoke env still wins.
func setIfUnset(m map[string]string, key, val string) {
	if m[key] == "" {
		m[key] = val
	}
}
