package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestInjectRequestSetsOnlyTheHeader(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "real-token"}
	req, err := http.NewRequest(http.MethodGet, "https://api.example.com/user?t=guest-dummy", strings.NewReader("body holds guest-dummy and must stay"))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-dummy")

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest reported no injection for a request that sent the header")
	}

	if got := req.Header.Get("Authorization"); got != "Bearer real-token" {
		t.Errorf("Authorization = %q, want the real credential", got)
	}
	// The old substring swap also rewrote query and path, so a guest could splice
	// the placeholder into a URL and get the credential reflected into a request
	// line the destination logs. Injection must never touch the URL.
	if !strings.Contains(req.URL.RawQuery, "guest-dummy") {
		t.Errorf("query was rewritten: %q; the credential must never reach a URL", req.URL.RawQuery)
	}
	body, _ := io.ReadAll(req.Body)
	if !strings.Contains(string(body), "guest-dummy") {
		t.Errorf("body should be untouched, got %q", body)
	}
}

func TestInjectRequestDoesNotAddAnAbsentHeader(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "real-token"}
	req, err := http.NewRequest(http.MethodGet, "https://api.example.com/health", nil)
	if err != nil {
		t.Fatal(err)
	}

	if injectRequest(req, sec) {
		t.Error("injected into a request that never sent the header")
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want empty; unrequested calls must stay uncredentialed", got)
	}
}

func TestInjectRequestDiscardsAGuestSuppliedToken(t *testing.T) {
	// A prompt-injected guest must not be able to authenticate as another account
	// by supplying a token of its own: whatever it sends is overwritten.
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "real-token"}
	req, err := http.NewRequest(http.MethodGet, "https://api.example.com/user", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer attacker-controlled-token")

	injectRequest(req, sec)

	if got := req.Header.Get("Authorization"); got != "Bearer real-token" {
		t.Errorf("Authorization = %q, want the guest value discarded", got)
	}
}

func TestInjectRequestHandlesEveryAuthorizationValue(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "real-token"}
	tests := []struct {
		name, wire string
		want       bool
	}{
		{"single header", "Authorization: guest\r\n", true},
		{"duplicate headers", "Authorization: first\r\nAuthorization: second\r\n", true},
		{"empty first plus nonempty second", "Authorization:\r\nAuthorization: Bearer attacker-own-token\r\n", true},
		{"mixed case spelling", "aUtHoRiZaTiOn: guest\r\n", true},
		// Presence, not content: a guest must not be able to suppress injection by
		// sending the header empty. This is the one behaviour the header-discard
		// change deliberately altered, so it is the one that needs pinning.
		{"single empty header", "Authorization:\r\n", true},
		{"absent header", "X-Test: present\r\n", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req, err := http.ReadRequest(bufio.NewReader(strings.NewReader(
				"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n" + tt.wire + "Content-Length: 0\r\n\r\n")))
			if err != nil {
				t.Fatal(err)
			}
			if got := injectRequest(req, sec); got != tt.want {
				t.Fatalf("injected = %v, want %v", got, tt.want)
			}
			values := req.Header.Values("Authorization")
			if tt.want && (len(values) != 1 || values[0] != "Bearer real-token") {
				t.Fatalf("Authorization values = %#v, want only the real credential", values)
			}
			if !tt.want && len(values) != 0 {
				t.Fatalf("Authorization values = %#v, want absent", values)
			}
		})
	}
}

func TestSwapPumpRejectsUnsupportedRequestModes(t *testing.T) {
	for _, tt := range []struct {
		name, request string
	}{
		{"expect", "POST /v1/messages HTTP/1.1\r\nHost: api.example.com\r\nExpect: 100-continue\r\nContent-Length: 0\r\n\r\n"},
		{"connect", "CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com\r\n\r\n"},
		{"connection upgrade", "GET / HTTP/1.1\r\nHost: api.example.com\r\nConnection: Upgrade\r\n\r\n"},
		{"upgrade header", "GET / HTTP/1.1\r\nHost: api.example.com\r\nUpgrade: websocket\r\n\r\n"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			upClient, upOrigin := net.Pipe()
			defer upClient.Close()
			defer upOrigin.Close()
			sec := &secretEntry{Header: "Authorization", value: "real"}
			p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
			p.swapPump(bufio.NewReader(strings.NewReader(tt.request)), io.Discard, upClient, "api.example.com", sec)
			_ = upOrigin.SetReadDeadline(time.Now().Add(20 * time.Millisecond))
			var b [1]byte
			if n, err := upOrigin.Read(b[:]); n != 0 || err == nil {
				t.Fatalf("unsupported request was forwarded: n=%d err=%v", n, err)
			}
		})
	}
}

type signalBuffer struct {
	mu       sync.Mutex
	data     bytes.Buffer
	contains chan struct{}
	signaled bool
}

func (w *signalBuffer) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	n, _ := w.data.Write(p)
	if !w.signaled && bytes.Contains(w.data.Bytes(), []byte("hello")) {
		w.signaled = true
		close(w.contains)
	}
	return n, nil
}

func (w *signalBuffer) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.data.String()
}

func TestSwapPumpRelaysStreamingResponseIncrementally(t *testing.T) {
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	defer upOrigin.Close()
	sec := &secretEntry{Header: "Authorization", value: "real"}
	guest := "GET http://api.example.com/stream HTTP/1.1\r\n" +
		"Host: api.example.com\r\nAuthorization: guest\r\nConnection: close\r\n\r\n"
	var back signalBuffer
	back.contains = make(chan struct{})
	done := make(chan struct{})
	go func() {
		p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
		p.swapPump(bufio.NewReader(strings.NewReader(guest)), &back, upClient, "api.example.com", sec)
		close(done)
	}()

	if _, err := http.ReadRequest(bufio.NewReader(upOrigin)); err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(upOrigin, "HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nhello"); err != nil {
		t.Fatal(err)
	}
	select {
	case <-back.contains:
		if !strings.Contains(back.String(), "hello") {
			t.Fatal("first response chunk was not relayed")
		}
	case <-time.After(time.Second):
		t.Fatal("first response chunk was not relayed incrementally")
	}
	if _, err := io.WriteString(upOrigin, "world"); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("swap pump did not finish after streaming response completed")
	}
	if !strings.HasSuffix(back.String(), "helloworld") {
		t.Fatalf("response = %q, want streamed body", back.String())
	}
}

func TestLoadSecretsFailsClosed(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	originalExit := exitFn
	t.Cleanup(func() { exitFn = originalExit })

	for _, tc := range []struct {
		name, catalog string
		wantExit      bool
	}{
		{"malformed json", "{not json", true},
		{"entry missing header", `[{"env":"TOK","egressTo":["api.example.com"]}]`, true},
		{"entry missing egressTo", `[{"header":"Authorization","env":"TOK"}]`, true},
		{"complete entry", `[{"header":"Authorization","env":"TOK","egressTo":["api.example.com"]}]`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			exited := false
			exitFn = func(int) { exited = true }
			t.Setenv("EGRESS_SECRETS", tc.catalog)
			t.Setenv("TOK", "real")
			loadSecrets(logger)
			if exited != tc.wantExit {
				t.Errorf("exited = %v, want %v; a bad catalog must not degrade to no catalog", exited, tc.wantExit)
			}
		})
	}
}

func TestLoadSecretsKeepsAnUnresolvedEntryDead(t *testing.T) {
	// Dropping it would make secretFor miss, and a guest on the cleartext lane
	// would then blind-tunnel its prompt over the public internet unencrypted.
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	originalExit := exitFn
	t.Cleanup(func() { exitFn = originalExit })
	exitFn = func(int) { t.Fatal("an unresolved secret must not exit; it would take down all egress on the node") }

	t.Setenv("EGRESS_SECRETS", `[{"header":"Authorization","env":"MISSING_TOK","egressTo":["api.example.com"]}]`)
	t.Setenv("MISSING_TOK", "")

	got := loadSecrets(logger)
	if len(got) != 1 {
		t.Fatalf("got %d entries, want the entry KEPT so its hosts can be denied", len(got))
	}
	if got[0].live() {
		t.Error("entry reports live with no resolved value")
	}
}

func TestSecretFor(t *testing.T) {
	p := &proxy{secrets: []secretEntry{{
		Env: "GITHUB_TOKEN", Header: "Authorization", value: "real", EgressTo: []string{"api.github.com", "github.com"},
	}}}
	if p.secretFor("API.GITHUB.COM") == nil {
		t.Error("expected case-insensitive egressTo match")
	}
	if p.secretFor("evil.com") != nil {
		t.Error("expected no match for unlisted host")
	}
}

func TestMinterMintsSignedLeaf(t *testing.T) {
	certFile, keyFile := writeTestCA(t)
	m, err := newCAMinter(certFile, keyFile)
	if err != nil {
		t.Fatalf("newCAMinter: %v", err)
	}

	cert, err := m.getCertificate(&tls.ClientHelloInfo{ServerName: "api.github.com"})
	if err != nil {
		t.Fatalf("getCertificate: %v", err)
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		t.Fatalf("parse leaf: %v", err)
	}
	if len(leaf.DNSNames) != 1 || leaf.DNSNames[0] != "api.github.com" {
		t.Errorf("leaf DNSNames = %v, want [api.github.com]", leaf.DNSNames)
	}
	if err := leaf.CheckSignatureFrom(m.caCert); err != nil {
		t.Errorf("leaf not signed by CA: %v", err)
	}
	// Second call for the same SNI returns the cached cert.
	cert2, _ := m.getCertificate(&tls.ClientHelloInfo{ServerName: "api.github.com"})
	if cert2 != cert {
		t.Error("expected cached certificate on repeat SNI")
	}
	// No SNI is an error (we cannot mint a leaf for an unknown name).
	if _, err := m.getCertificate(&tls.ClientHelloInfo{}); err == nil {
		t.Error("expected error for empty SNI")
	}
}

// writeTestCA generates a throwaway CA and writes its cert + key to temp PEM files.
func writeTestCA(t *testing.T) (certFile, keyFile string) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test egress CA"},
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	certFile = dir + "/ca.crt"
	keyFile = dir + "/ca.key"
	if err := os.WriteFile(certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyFile, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0o600); err != nil {
		t.Fatal(err)
	}
	return certFile, keyFile
}

// TestSwapPumpPlaintextLane covers the lane the embervm claude runtime uses: the
// guest addresses the sidecar as a proxy in cleartext, so the request arrives in
// absolute-form carrying the inert placeholder. The sidecar must swap it, re-emit
// origin-form, and hand the response back.
func TestSwapPumpInjectsOnThePlaintextLane(t *testing.T) {
	sec := &secretEntry{
		Header:      "Authorization",
		ValuePrefix: "Bearer ",
		value:       "injected-real-value",
		EgressTo:    []string{"api.example.com"},
	}

	upClient, upOrigin := net.Pipe()
	defer upClient.Close()

	seen := make(chan *http.Request, 1)
	go func() {
		defer upOrigin.Close()
		req, err := http.ReadRequest(bufio.NewReader(upOrigin))
		if err != nil {
			seen <- nil
			return
		}
		seen <- req
		_, _ = io.WriteString(upOrigin, "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
	}()

	guest := "POST http://api.example.com/v1/messages HTTP/1.1\r\n" +
		"Host: api.example.com\r\n" +
		"Authorization: Bearer guest-login-gate-dummy\r\n" +
		"Content-Length: 0\r\n" +
		"Connection: close\r\n\r\n"

	var back bytes.Buffer
	p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	p.swapPump(bufio.NewReader(strings.NewReader(guest)), &back, upClient, "api.example.com", sec)

	got := <-seen
	if got == nil {
		t.Fatal("origin never read a request")
	}
	if auth := got.Header.Get("Authorization"); auth != "Bearer injected-real-value" {
		t.Errorf("Authorization = %q, want the real value injected", auth)
	}
	// The origin must see origin-form, not the proxy's absolute-form request line.
	if got.RequestURI != "/v1/messages" {
		t.Errorf("RequestURI = %q, want origin-form /v1/messages", got.RequestURI)
	}
	if got.Host != "api.example.com" {
		t.Errorf("Host = %q, want api.example.com", got.Host)
	}
	if !strings.Contains(back.String(), "200 OK") || !strings.HasSuffix(back.String(), "hi") {
		t.Errorf("response not relayed to the guest: %q", back.String())
	}
}
