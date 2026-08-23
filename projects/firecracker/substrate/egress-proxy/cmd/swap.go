package main

// Credential injection on egress (ADR 023 6b). For a destination that carries a
// credential, the sidecar reads the plaintext request, sets the configured header
// to the real secret value (mounted only here, never in the guest), and originates
// a fresh verified TLS connection to the real destination. Injection fires only
// when the request's destination is in that secret's egressTo, so the credential
// is unreachable at every other host.
//
// This supersedes the original placeholder-substitution design. That scheme needed
// the same byte string present in the guest AND in this catalog, a coupling that
// could only be kept honest by a test policing two copies, and it substituted over
// headers, query and path, so a guest could splice the placeholder into a URL and
// get the real credential reflected into a request line.

import (
	"bufio"
	"bytes"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const brokerRefreshMargin = 60 * time.Second

const maxHeaderBytes = 64 << 10

var headerTimeout = 30 * time.Second

type brokerTokenResponse struct {
	AccessToken string    `json:"access_token"`
	ExpiresAt   time.Time `json:"expires_at"`
}

type brokerGrantState struct {
	mu        sync.Mutex
	token     string
	expiresAt time.Time
}

type tokenBroker struct {
	baseURL string
	client  *http.Client
	mu      sync.Mutex
	grants  map[string]*brokerGrantState
}

func newTokenBroker(rawURL string) *tokenBroker {
	if rawURL == "" {
		return &tokenBroker{grants: make(map[string]*brokerGrantState)}
	}
	if !strings.Contains(rawURL, "://") {
		rawURL = "http://" + rawURL
	}
	return &tokenBroker{
		baseURL: strings.TrimRight(rawURL, "/"),
		client:  &http.Client{Timeout: 10 * time.Second},
		grants:  make(map[string]*brokerGrantState),
	}
}

func (b *tokenBroker) state(grant string) *brokerGrantState {
	b.mu.Lock()
	defer b.mu.Unlock()
	if s, ok := b.grants[grant]; ok {
		return s
	}
	s := &brokerGrantState{}
	b.grants[grant] = s
	return s
}

func (b *tokenBroker) token(grant string) (string, time.Time, error) {
	state := b.state(grant)
	state.mu.Lock()
	defer state.mu.Unlock()
	if state.token != "" && time.Now().Before(state.expiresAt.Add(-brokerRefreshMargin)) {
		return state.token, state.expiresAt, nil
	}
	if b.baseURL == "" {
		return "", time.Time{}, fmt.Errorf("token broker URL is empty")
	}
	endpoint := b.baseURL + "/grants/" + url.PathEscape(grant) + "/token"
	resp, err := b.client.Get(endpoint)
	if err != nil {
		state.token, state.expiresAt = "", time.Time{}
		return "", time.Time{}, fmt.Errorf("get grant %q: %w", grant, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		state.token, state.expiresAt = "", time.Time{}
		return "", time.Time{}, fmt.Errorf("get grant %q: broker returned %s", grant, resp.Status)
	}
	var result brokerTokenResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil || result.AccessToken == "" || result.ExpiresAt.IsZero() {
		state.token, state.expiresAt = "", time.Time{}
		if err == nil {
			err = fmt.Errorf("missing access_token or expires_at")
		}
		return "", time.Time{}, fmt.Errorf("decode grant %q: %w", grant, err)
	}
	state.token, state.expiresAt = result.AccessToken, result.ExpiresAt
	return state.token, state.expiresAt, nil
}

// secretEntry is one catalog entry: on a connection to a host in EgressTo, the
// sidecar sets Header to ValuePrefix+value, where value is resolved from the
// sidecar's own env (mounted from a Secret) at startup.
//
// This REPLACES the placeholder-substitution scheme. A substring swap needs the
// same byte string in the guest and in this catalog, forever, so it could only be
// kept honest by a drift test policing two copies. It was also looser than it
// looked: the swap ran over headers, query AND path, so a guest could splice the
// placeholder into a URL and get the real credential reflected into a request
// line, where it lands in the destination's server logs. Injection confines the
// credential to one header and deletes the coupling.
type secretEntry struct {
	// Header is the request header to set, e.g. "Authorization".
	Header string `json:"header"`
	// ValuePrefix is prepended to the resolved value, e.g. "Bearer ".
	ValuePrefix string `json:"valuePrefix"`
	// BasicUser, when set, emits RFC 7617 Basic instead of ValuePrefix+value:
	// "Basic " + base64(BasicUser + ":" + value).
	//
	// This exists because git over HTTPS and the GitHub API want the SAME token
	// in two different shapes. Verified against github.com with a real PAT:
	//
	//   git-receive-pack + "Authorization: Bearer <tok>"                 -> 401
	//   git-receive-pack + "Authorization: Basic base64(user:<tok>)"     -> 200
	//   api.github.com   + "Authorization: Bearer <tok>"                 -> 200
	//
	// Encoding HERE rather than storing a pre-encoded second secret keeps ONE
	// credential in the vault. A stored base64 blob would be a derived copy that
	// silently outlives a rotation of the value it was derived from, and nothing
	// would detect the drift until a push started failing.
	BasicUser   string   `json:"basicUser"`
	Env         string   `json:"env"`
	BrokerGrant string   `json:"brokerGrant"`
	EgressTo    []string `json:"egressTo"`
	ClaimHeader string   `json:"claimHeader"`
	ClaimPath   string   `json:"claimPath"`
	// InjectAlwaysPaths is the explicit substitute signal for a client that
	// sends NO credential header at all, so presence cannot signal intent for
	// it (issue #4298: the codex CLI's rmcp connector client POSTs with no
	// Authorization header). An exact match of the request's URL path against
	// this list stands in for header presence, and only for that. Everything
	// else about the entry stays fail-closed: an unlisted path with no header
	// still denies, and a claim-configured entry that cannot resolve its claim
	// still denies even on a listed path.
	InjectAlwaysPaths []string `json:"injectAlwaysPaths"`
	value             string   // resolved real value; never serialized, never logged
	expiresAt         time.Time
	broker            *tokenBroker
	mu                *sync.RWMutex
}

// injectAlwaysPath reports whether path is one of this entry's
// InjectAlwaysPaths (issue #4298's operator opt-in for a header-free
// connector client), an exact match against req.URL.Path.
func (e *secretEntry) injectAlwaysPath(path string) bool {
	for _, p := range e.InjectAlwaysPaths {
		// An empty entry (a bare "-" list item rendering as null -> "") is a
		// config error, not a wildcard: skip it so it cannot match a path-less
		// (e.g. CONNECT) request and inject the credential.
		if p == "" {
			continue
		}
		if p == path {
			return true
		}
	}
	return false
}

// live reports whether this entry can actually inject. A catalog entry whose
// secret has not resolved is kept (not dropped) so handle() can DENY its hosts;
// dropping it would make secretFor miss, and a guest on the cleartext lane would
// then fall through to the blind tunnel and send its prompt over the public
// internet unencrypted.
func (e *secretEntry) live() bool {
	if e.mu != nil {
		e.mu.RLock()
		defer e.mu.RUnlock()
	}
	return e.value != ""
}

func (e *secretEntry) resolve() error {
	if e.BrokerGrant == "" {
		return nil
	}
	if e.broker == nil {
		if e.mu == nil {
			e.mu = &sync.RWMutex{}
		}
		e.mu.Lock()
		e.value, e.expiresAt = "", time.Time{}
		e.mu.Unlock()
		return fmt.Errorf("token broker is not configured")
	}
	token, expiresAt, err := e.broker.token(e.BrokerGrant)
	if err != nil {
		if e.mu == nil {
			e.mu = &sync.RWMutex{}
		}
		e.mu.Lock()
		e.value, e.expiresAt = "", time.Time{}
		e.mu.Unlock()
		return err
	}
	if e.mu == nil {
		e.mu = &sync.RWMutex{}
	}
	e.mu.Lock()
	e.value, e.expiresAt = token, expiresAt
	e.mu.Unlock()
	return nil
}

// headerValue renders what the sidecar SETS on the request: Basic when the
// entry names a user, otherwise the prefix form. One place, so the two callers
// (claim-bearing and plain) cannot diverge on encoding.
func (e *secretEntry) headerValue() string {
	value := e.resolvedValue()
	if e.BasicUser != "" {
		return "Basic " + base64.StdEncoding.EncodeToString([]byte(e.BasicUser+":"+value))
	}
	return e.ValuePrefix + value
}

func (e *secretEntry) resolvedValue() string {
	if e.mu != nil {
		e.mu.RLock()
		defer e.mu.RUnlock()
	}
	return e.value
}

// loadSecrets parses EGRESS_SECRETS (a JSON catalog) and resolves each real value
// from the sidecar env.
//
// FAIL CLOSED, in two different ways on purpose:
//
//   - A malformed catalog is a DEPLOY-time config error, so exit non-zero and let
//     it surface loudly in ArgoCD rather than run on with no catalog. Returning
//     nil here is what previously turned a typo into silent cleartext egress.
//   - A well-formed entry whose secret env is empty is a RUNTIME hiccup, so keep
//     the entry (dead) and deny only its own egressTo hosts. Exiting instead would
//     crash-loop the sidecar and take down all guest egress on the node over one
//     unresolved credential.
func loadSecrets(logger *slog.Logger) []secretEntry {
	return loadSecretsWithBroker(logger, os.Getenv("EGRESS_TOKEN_BROKER_URL"))
}

func loadSecretsWithBroker(logger *slog.Logger, brokerURL string) []secretEntry {
	raw := os.Getenv("EGRESS_SECRETS")
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var entries []secretEntry
	if err := json.Unmarshal([]byte(raw), &entries); err != nil {
		logger.Error("EGRESS_SECRETS is not valid JSON; refusing to start", "err", err)
		exitFn(1)
		return nil
	}
	broker := newTokenBroker(brokerURL)
	out := make([]secretEntry, 0, len(entries))
	for _, e := range entries {
		hasSecret := (e.Env != "") != (e.BrokerGrant != "")
		if e.Header == "" || len(e.EgressTo) == 0 || !hasSecret {
			logger.Error("invalid secret catalog entry (needs exactly one of env or brokerGrant, plus header and egressTo); refusing to start", "env", e.Env, "brokerGrant", e.BrokerGrant)
			exitFn(1)
			return nil
		}
		// basicUser and valuePrefix are two different encodings of the same
		// header, so setting both is a config error rather than a precedence
		// question. Failing at load beats guessing and injecting the wrong shape,
		// which surfaces later as an opaque 401 from the destination.
		if e.BasicUser != "" && e.ValuePrefix != "" {
			logger.Error("secret catalog entry sets both basicUser and valuePrefix; refusing to start", "egressTo", e.EgressTo)
			exitFn(1)
			return nil
		}
		if e.BrokerGrant != "" {
			e.broker = broker
			e.mu = &sync.RWMutex{}
			if err := e.resolve(); err != nil {
				logger.Error("broker token empty; its egressTo hosts will be DENIED", "grant", e.BrokerGrant, "egressTo", e.EgressTo, "err", err)
			}
		} else {
			e.value = os.Getenv(e.Env)
		}
		if !e.live() {
			// Secret-backed environment variables are fixed for the lifetime of a
			// container. A pod restart is required after the secret resolves.
			logger.Error("secret env empty; its egressTo hosts will be DENIED; restart the pod after it resolves", "env", e.Env, "egressTo", e.EgressTo)
		}
		out = append(out, e)
	}
	return out
}

// secretFor returns the catalog entry whose egressTo includes host (exact,
// case-insensitive), or nil. host is the bare hostname (no port).
func (p *proxy) secretFor(host string) *secretEntry {
	for i := range p.secrets {
		for _, allowed := range p.secrets[i].EgressTo {
			if strings.EqualFold(host, allowed) {
				return &p.secrets[i]
			}
		}
	}
	return nil
}

// caMinter terminates guest TLS by minting leaf certs signed by the egress CA.
// The CA private key lives only here; one leaf key is generated at startup and
// reused for every minted cert (the slow key-gen happens once, signing is cheap).
type caMinter struct {
	caCert  *x509.Certificate
	caKey   crypto.Signer
	leafKey *rsa.PrivateKey
	mu      sync.Mutex
	cache   map[string]*tls.Certificate
}

// newCAMinter loads the CA cert + key (cert-manager Secret, PEM) and pre-generates
// the shared leaf key.
func newCAMinter(certFile, keyFile string) (*caMinter, error) {
	certPEM, err := os.ReadFile(certFile)
	if err != nil {
		return nil, fmt.Errorf("read CA cert: %w", err)
	}
	keyPEM, err := os.ReadFile(keyFile)
	if err != nil {
		return nil, fmt.Errorf("read CA key: %w", err)
	}
	caTLS, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse CA keypair: %w", err)
	}
	caCert, err := x509.ParseCertificate(caTLS.Certificate[0])
	if err != nil {
		return nil, fmt.Errorf("parse CA cert: %w", err)
	}
	caKey, ok := caTLS.PrivateKey.(crypto.Signer)
	if !ok {
		return nil, fmt.Errorf("CA key is not a crypto.Signer")
	}
	leafKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, fmt.Errorf("generate leaf key: %w", err)
	}
	return &caMinter{caCert: caCert, caKey: caKey, leafKey: leafKey, cache: map[string]*tls.Certificate{}}, nil
}

// getCertificate is the tls.Config.GetCertificate callback: it returns a leaf for
// the requested SNI host, minting and caching it on first use.
func (m *caMinter) getCertificate(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
	host := hello.ServerName
	if host == "" {
		return nil, fmt.Errorf("no SNI in ClientHello")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if c, ok := m.cache[host]; ok {
		return c, nil
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, err
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: host},
		DNSNames:     []string{host},
		NotBefore:    now.Add(-5 * time.Minute),
		NotAfter:     now.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, m.caCert, &m.leafKey.PublicKey, m.caKey)
	if err != nil {
		return nil, fmt.Errorf("mint leaf for %s: %w", host, err)
	}
	cert := &tls.Certificate{Certificate: [][]byte{der, m.caCert.Raw}, PrivateKey: m.leafKey}
	m.cache[host] = cert
	return cert, nil
}

// caCertPEM returns the CA certificate in PEM form (public, for injecting into
// the guest trust store).
func (m *caMinter) caCertPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: m.caCert.Raw})
}

// prefixConn presents a net.Conn whose reads come from a bufio.Reader (which holds
// the peeked-but-unconsumed ClientHello) while writes/close/deadlines go to the
// underlying conn, so tls.Server sees the exact original handshake bytes.
type prefixConn struct {
	r io.Reader
	net.Conn
}

func (c prefixConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// swapPlaintext serves a guest that speaks http:// to the sidecar over its
// host-local vsock. There is no guest TLS to terminate, so the sidecar reads the
// requests directly and originates the real TLS upstream itself. dialAddr must
// already be the guardrail-pinned 443 address for host.
func (p *proxy) swapPlaintext(br *bufio.Reader, client net.Conn, dialAddr, host string, sec *secretEntry) {
	up, err := tls.DialWithDialer(&net.Dialer{Timeout: dialTimeout}, "tcp", dialAddr, &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
	if err != nil {
		p.logger.Error("egress swap: upstream TLS dial failed", "host", host, "dial", dialAddr, "err", err)
		return
	}
	// Git's protocol is request/response in small messages. Nagle holds each
	// write until the prior segment is ACKed, while delayed ACK waits up to
	// 40ms. This measured as ~55ms per 64 KiB chunk and about 10 seconds added
	// to an 11.24 MiB clone, so disable Nagle on the underlying TCP socket.
	if netConn, ok := up.NetConn().(*net.TCPConn); ok {
		_ = netConn.SetNoDelay(true)
	}
	defer up.Close()
	p.swapPump(br, client, client, up, host, sec)
}

// terminateAndSwap MITMs a TLS connection to a secret-bearing destination: it
// terminates the guest's TLS with a minted leaf and re-originates over a freshly
// validated TLS connection to the pinned dialAddr. The cert is validated against
// host (ServerName), so dialing the guardrail-pinned IP does not weaken TLS
// verification. It returns when either side closes.
func (p *proxy) terminateAndSwap(br *bufio.Reader, client net.Conn, dialAddr, host string, sec *secretEntry) {
	serverCfg := &tls.Config{GetCertificate: p.minter.getCertificate, MinVersion: tls.VersionTLS12}
	guest := tls.Server(prefixConn{r: br, Conn: client}, serverCfg)
	defer guest.Close()
	if err := guest.Handshake(); err != nil {
		p.logger.Warn("egress swap: guest TLS handshake failed", "host", host, "err", err)
		return
	}

	up, err := tls.DialWithDialer(&net.Dialer{Timeout: dialTimeout}, "tcp", dialAddr, &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
	if err != nil {
		p.logger.Error("egress swap: upstream TLS dial failed", "host", host, "dial", dialAddr, "err", err)
		return
	}
	// Git's protocol is request/response in small messages. Nagle holds each
	// write until the prior segment is ACKed, while delayed ACK waits up to
	// 40ms. This measured as ~55ms per 64 KiB chunk and about 10 seconds added
	// to an 11.24 MiB clone, so disable Nagle on the underlying TCP socket.
	if netConn, ok := up.NetConn().(*net.TCPConn); ok {
		_ = netConn.SetNoDelay(true)
	}
	defer up.Close()

	p.swapPump(bufio.NewReader(guest), guest, guest, up, host, sec)
}

// swapPump relays HTTP requests from an already-plaintext guest stream to up,
// injecting the credential into each one. Only the configured header is touched:
// never the body (so there is no Content-Length to recompute), and never the URL,
// so the credential cannot end up in a request line the destination logs.
// guestR carries the guest's request bytes and guestW takes the responses; they
// are the same connection, split so the TLS and plaintext lanes can share this.
// It returns when either side closes, or when a request asks to close.
func (p *proxy) swapPump(guestR *bufio.Reader, guestW io.Writer, guestDeadline interface{ SetReadDeadline(time.Time) error }, up net.Conn, host string, sec *secretEntry) {
	upR := bufio.NewReader(up)
	guestStream := &replayReader{source: guestR}
	for {
		if guestDeadline != nil {
			if err := guestDeadline.SetReadDeadline(time.Now().Add(headerTimeout)); err != nil {
				p.logger.Debug("egress swap: set header deadline", "dest", host, "err", err)
				return
			}
		}
		headerR := bufio.NewReaderSize(guestStream, maxHeaderBytes+1)
		headerBytes, tooLarge, err := readRequestHeader(headerR)
		if tooLarge {
			p.logger.Warn("egress swap: header too large; closing connection", "dest", host, "limit", maxHeaderBytes)
			return
		}
		guestStream.prependBuffered(headerR)
		var (
			req      *http.Request
			requestR *bufio.Reader
		)
		if err == nil {
			guestStream.prepend(headerBytes)
			requestR = bufio.NewReaderSize(guestStream, maxHeaderBytes+1)
			req, err = http.ReadRequest(requestR)
		}
		if err != nil {
			if err != io.EOF {
				p.logger.Debug("egress swap: read request", "dest", host, "err", err)
			}
			return
		}
		if guestDeadline != nil {
			if err := guestDeadline.SetReadDeadline(time.Time{}); err != nil {
				p.logger.Debug("egress swap: clear header deadline", "dest", host, "err", err)
				return
			}
		}
		if authority := mismatchedAuthority(req, headerBytes, host); authority != "" {
			p.logger.Warn("egress swap: authority mismatch; request denied", "dest", host, "authority", authority)
			return
		}
		req.Host = host
		if rejectSwapRequest(req) {
			p.logger.Warn("egress swap: unsupported request mode; closing connection", "dest", host, "method", req.Method)
			return
		}
		if credentialInTrailer(req, sec) {
			p.logger.Warn("egress swap: credential trailer denied", "dest", host, "header", sec.Header)
			return
		}
		injected := injectRequest(req, sec)
		if sec.ClaimHeader != "" && !injected {
			p.logger.Warn("egress swap: claim injection failed; request denied", "dest", host, "header", sec.ClaimHeader, "status", "denied")
			return
		}
		// A guest on the plaintext lane addresses us as a proxy, so its request line
		// is absolute-form ("POST http://host/path"). Clearing RequestURI and the
		// URL's scheme/host makes req.Write emit origin-form with a Host header,
		// which is what an origin server expects. req.Host was already populated
		// from whichever form arrived, so the header survives either way.
		closeAfter := req.Close
		req.RequestURI = ""
		req.URL.Scheme, req.URL.Host = "", ""
		if err := req.Write(up); err != nil {
			p.logger.Warn("egress swap: forward request", "dest", host, "err", err)
			return
		}
		guestStream.prependBuffered(requestR)
		resp, err := http.ReadResponse(upR, req)
		// Skip interim responses. Rejecting Expect only stops the guest SOLICITING
		// a 1xx; a destination can still send 103 Early Hints unasked, and
		// http.ReadResponse does not skip them (that lives in Transport's read
		// loop, not the parser). Relayed, a 103 becomes the guest's answer and the
		// real final response gets paired with whatever request arrives next.
		// api.anthropic.com does not do this today, but egressTo is per-secret, so
		// every future credentialed host would inherit it.
		for err == nil && resp.StatusCode >= 100 && resp.StatusCode < 200 {
			_ = resp.Body.Close()
			resp, err = http.ReadResponse(upR, req)
		}
		if err != nil {
			p.logger.Warn("egress swap: read response", "dest", host, "err", err)
			return
		}
		err = resp.Write(guestW)
		_ = resp.Body.Close()
		if err != nil {
			p.logger.Warn("egress swap: return response", "dest", host, "err", err)
			return
		}
		p.logger.Info("egress injected", "dest", host, "header", sec.Header, "injected", injected, "path", req.URL.Path, "status", resp.StatusCode)
		// Honour the guest's Connection: close rather than blocking on a request it
		// has already told us will not come; the caller's defer closes both sides.
		if closeAfter {
			return
		}
	}
}

func readRequestHeader(guestR *bufio.Reader) ([]byte, bool, error) {
	capacity := guestR.Size()
	if capacity > maxHeaderBytes {
		capacity = maxHeaderBytes
	}
	header := make([]byte, 0, capacity)
	lineBytes := 0
	for {
		fragment, err := guestR.ReadSlice('\n')
		if len(header)+len(fragment) > maxHeaderBytes {
			return nil, true, nil
		}
		header = append(header, fragment...)
		if err == bufio.ErrBufferFull {
			lineBytes += len(fragment)
			continue
		}
		if err != nil {
			return nil, false, err
		}
		if lineBytes == 0 && (bytes.Equal(fragment, []byte("\r\n")) || bytes.Equal(fragment, []byte("\n"))) {
			return header, false, nil
		}
		lineBytes = 0
	}
}

// replayReader lets each bounded-header parse and http.ReadRequest use a fresh
// bufio.Reader without losing bytes that parser read ahead from the next body or
// keep-alive request. prependBuffered returns that read-ahead to the single
// connection stream before the short-lived parser is discarded.
type replayReader struct {
	source  io.Reader
	pending []byte
}

func (r *replayReader) Read(p []byte) (int, error) {
	if len(r.pending) > 0 {
		n := copy(p, r.pending)
		r.pending = r.pending[n:]
		return n, nil
	}
	return r.source.Read(p)
}

func (r *replayReader) prepend(data []byte) {
	if len(data) == 0 {
		return
	}
	pending := make([]byte, 0, len(data)+len(r.pending))
	pending = append(pending, data...)
	pending = append(pending, r.pending...)
	r.pending = pending
}

func (r *replayReader) prependBuffered(br *bufio.Reader) {
	if br.Buffered() == 0 {
		return
	}
	buffered, _ := br.Peek(br.Buffered())
	r.prepend(buffered)
}

func mismatchedAuthority(req *http.Request, rawHeader []byte, policyHost string) string {
	want := canonicalAuthority(policyHost)
	authorities := []string{req.Host}
	if req.URL != nil {
		authorities = append(authorities, req.URL.Host)
	}
	if host := rawHostHeader(rawHeader); host != "" {
		authorities = append(authorities, host)
	}
	for _, authority := range authorities {
		if authority != "" && canonicalAuthority(authority) != want {
			return authority
		}
	}
	return ""
}

func rawHostHeader(rawHeader []byte) string {
	r := textproto.NewReader(bufio.NewReader(bytes.NewReader(rawHeader)))
	if _, err := r.ReadLine(); err != nil {
		return ""
	}
	header, err := r.ReadMIMEHeader()
	if err != nil {
		return ""
	}
	return header.Get("Host")
}

func canonicalAuthority(authority string) string {
	if host, _, err := net.SplitHostPort(authority); err == nil {
		authority = host
	}
	return strings.TrimSuffix(strings.ToLower(authority), ".")
}

func credentialInTrailer(req *http.Request, sec *secretEntry) bool {
	for key := range req.Trailer {
		if strings.EqualFold(key, sec.Header) || (sec.ClaimHeader != "" && strings.EqualFold(key, sec.ClaimHeader)) {
			return true
		}
	}
	return false
}

// rejectSwapRequest keeps the swap lane strictly request-then-response. Expect
// and informational responses require concurrent pumping, while CONNECT and
// Upgrade switch protocols and cannot be represented by this loop.
func rejectSwapRequest(req *http.Request) bool {
	if len(req.Header.Values("Expect")) > 0 || req.Method == http.MethodConnect {
		return true
	}
	if len(req.Header.Values("Upgrade")) > 0 {
		return true
	}
	for _, value := range req.Header.Values("Connection") {
		for _, token := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(token), "upgrade") {
				return true
			}
		}
	}
	return false
}

// injectRequest sets the entry's header to the real credential.
//
// Only that one header, and only when the guest sent it AT ALL: presence is the
// signal, not content, so an empty value counts. The guest has to send something
// there anyway (the claude CLI refuses to make any request until it believes it is
// logged in), and keying on presence stops the sidecar silently credentialing
// requests the client never meant to authenticate, without letting a guest suppress
// injection by sending an empty value. Query, path and
// every other header are left alone, which is the leak the substring swap had.
//
// A connector-style client can send no credential header at all (issue #4298:
// the codex CLI's rmcp connector client POSTs with no Authorization header on
// either rust-v0.146.0 or 0.147.0-alpha.6), so presence can never signal intent
// for it. sec.InjectAlwaysPaths is the operator's explicit substitute signal for
// exactly that case: an exact match of the request path stands in for presence,
// and only for that request. Every other path on the same entry, and every
// request on an entry with no InjectAlwaysPaths, still keys on presence exactly
// as before.
//
// Whatever the guest put in the header is DISCARDED rather than matched. That is
// the point: the guest's value is uncoupled config, and a prompt-injected guest
// cannot authenticate as a different account by supplying its own token.
func injectRequest(req *http.Request, sec *secretEntry) bool {
	if sec.ClaimHeader != "" {
		requested := len(req.Header.Values(sec.Header)) > 0 || sec.injectAlwaysPath(req.URL.Path)
		// Both guest values go before either decision, so a prompt-injected guest
		// cannot keep its own account id by making the credential lookup fail.
		req.Header.Del(sec.Header)
		req.Header.Del(sec.ClaimHeader)
		if !requested || sec.ClaimPath == "" {
			return false
		}
		claim, ok := jwtClaim(sec.resolvedValue(), sec.ClaimPath)
		if !ok {
			return false
		}
		// BOTH headers, always together. The credential and the account id come
		// from the same token by construction, which is the point of sourcing the
		// claim here rather than trusting the guest: they cannot drift, and a
		// valid token paired with a stale account id is exactly the failure this
		// fixes (the provider rejects the pair and calls it token_expired).
		req.Header.Set(sec.Header, sec.headerValue())
		req.Header.Set(sec.ClaimHeader, claim)
		return true
	}

	requested := len(req.Header.Values(sec.Header)) > 0 || sec.injectAlwaysPath(req.URL.Path)
	// Delete every guest value, including duplicate and empty values, before
	// deciding whether the guest requested credential injection.
	req.Header.Del(sec.Header)
	if !requested {
		return false
	}
	req.Header.Set(sec.Header, sec.headerValue())
	return true
}

func jwtClaim(token, path string) (string, bool) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", false
	}
	payload, err := decodeJWTPart(parts[1])
	if err != nil {
		return "", false
	}
	var claims map[string]any
	if json.Unmarshal(payload, &claims) != nil {
		return "", false
	}
	value, ok := findClaim(claims, path)
	if !ok {
		return "", false
	}
	claim, ok := value.(string)
	return claim, ok && claim != ""
}

func decodeJWTPart(part string) ([]byte, error) {
	if payload, err := base64.RawURLEncoding.DecodeString(part); err == nil {
		return payload, nil
	}
	return base64.URLEncoding.DecodeString(part)
}

func findClaim(value any, path string) (any, bool) {
	if path == "" {
		return value, true
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, false
	}
	if exact, ok := object[path]; ok {
		return exact, true
	}
	for i := len(path) - 1; i >= 0; i-- {
		if path[i] != '.' {
			continue
		}
		if prefix, ok := object[path[:i]]; ok {
			return findClaim(prefix, path[i+1:])
		}
	}
	return nil, false
}
