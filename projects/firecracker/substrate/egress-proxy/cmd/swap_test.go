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
