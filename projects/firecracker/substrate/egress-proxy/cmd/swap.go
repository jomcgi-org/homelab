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
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

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
	ValuePrefix string   `json:"valuePrefix"`
	Env         string   `json:"env"`
	EgressTo    []string `json:"egressTo"`
	value       string   // resolved real value; never serialized, never logged
}

// live reports whether this entry can actually inject. A catalog entry whose
// secret has not resolved is kept (not dropped) so handle() can DENY its hosts;
// dropping it would make secretFor miss, and a guest on the cleartext lane would
// then fall through to the blind tunnel and send its prompt over the public
// internet unencrypted.
func (e *secretEntry) live() bool { return e.value != "" }

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
	out := make([]secretEntry, 0, len(entries))
	for _, e := range entries {
		if e.Header == "" || e.Env == "" || len(e.EgressTo) == 0 {
			logger.Error("incomplete secret catalog entry (needs header, env, egressTo); refusing to start", "env", e.Env)
			exitFn(1)
			return nil
		}
		e.value = os.Getenv(e.Env)
		if !e.live() {
			logger.Error("secret env empty; its egressTo hosts will be DENIED until it resolves", "env", e.Env, "egressTo", e.EgressTo)
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
	defer up.Close()
	p.swapPump(br, client, up, host, sec)
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
	defer up.Close()

	p.swapPump(bufio.NewReader(guest), guest, up, host, sec)
}

// swapPump relays HTTP requests from an already-plaintext guest stream to up,
// injecting the credential into each one. Only the configured header is touched:
// never the body (so there is no Content-Length to recompute), and never the URL,
// so the credential cannot end up in a request line the destination logs.
// guestR carries the guest's request bytes and guestW takes the responses; they
// are the same connection, split so the TLS and plaintext lanes can share this.
// It returns when either side closes, or when a request asks to close.
func (p *proxy) swapPump(guestR *bufio.Reader, guestW io.Writer, up net.Conn, host string, sec *secretEntry) {
	upR := bufio.NewReader(up)
	for {
		req, err := http.ReadRequest(guestR)
		if err != nil {
			if err != io.EOF {
				p.logger.Debug("egress swap: read request", "dest", host, "err", err)
			}
			return
		}
		injected := injectRequest(req, sec)
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
		resp, err := http.ReadResponse(upR, req)
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
		p.logger.Info("egress injected", "dest", host, "header", sec.Header, "injected", injected, "path", req.URL.Path)
		// Honour the guest's Connection: close rather than blocking on a request it
		// has already told us will not come; the caller's defer closes both sides.
		if closeAfter {
			return
		}
	}
}

// injectRequest sets the entry's header to the real credential.
//
// Only that one header, and only when the guest already sent it: the guest has to
// send SOMETHING there anyway (the claude CLI refuses to make any request until it
// believes it is logged in), so requiring it keeps the sidecar from silently
// credentialing requests the client never meant to authenticate. Query, path and
// every other header are left alone, which is the leak the substring swap had.
//
// Whatever the guest put in the header is DISCARDED rather than matched. That is
// the point: the guest's value is uncoupled config, and a prompt-injected guest
// cannot authenticate as a different account by supplying its own token.
func injectRequest(req *http.Request, sec *secretEntry) bool {
	if req.Header.Get(sec.Header) == "" {
		return false
	}
	req.Header.Set(sec.Header, sec.ValuePrefix+sec.value)
	return true
}
