package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
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

func TestInjectClaimHeaderSetsValueFromToken(t *testing.T) {
	sec := &secretEntry{
		Header: "Authorization",
		// Mirrors the live catalog entry: the claim path is the real one, dots and
		// all, and the prefix is what production actually sends.
		ValuePrefix: "Bearer ",
		ClaimHeader: "chatgpt-account-id",
		ClaimPath:   "https://api.openai.com/auth.chatgpt_account_id",
		value:       testJWT(`{"https://api.openai.com/auth":{"chatgpt_account_id":"account-123"}}`),
	}
	req, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-token")

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest denied a valid JWT claim")
	}
	if got := req.Header.Get("chatgpt-account-id"); got != "account-123" {
		t.Errorf("chatgpt-account-id = %q, want the extracted account ID", got)
	}
	// The credential MUST still be injected on the claim path. An earlier draft of
	// this branch set only the account id, silently dropping the token, and a test
	// that asserted the new header alone was happy to let that through.
	if got := req.Header.Get("Authorization"); got != sec.ValuePrefix+sec.value {
		t.Errorf("Authorization = %q, want the injected credential, not the guest value", got)
	}
}

func TestInjectClaimHeaderDeletesGuestValue(t *testing.T) {
	sec := &secretEntry{
		Header:      "Authorization",
		ClaimHeader: "chatgpt-account-id",
		ClaimPath:   "account_id",
		value:       testJWT(`{"account_id":"real-account"}`),
	}
	req, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-token")
	req.Header.Set("chatgpt-account-id", "guest-account")

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest denied a valid JWT claim")
	}
	if got := req.Header.Get("chatgpt-account-id"); got != "real-account" {
		t.Errorf("chatgpt-account-id = %q, want the guest value discarded", got)
	}
}

func TestInjectClaimHeaderDeniesOnMissingClaim(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ClaimHeader: "chatgpt-account-id", ClaimPath: "account_id", value: testJWT(`{"other":"value"}`)}
	req, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-token")
	req.Header.Set("chatgpt-account-id", "guest-account")

	if injectRequest(req, sec) {
		t.Fatal("injectRequest allowed a request with a missing claim")
	}
	if got := req.Header.Get("chatgpt-account-id"); got != "" {
		t.Errorf("chatgpt-account-id = %q, want the header deleted on denial", got)
	}
}

func TestInjectClaimHeaderDeniesOnInvalidJWT(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ClaimHeader: "chatgpt-account-id", ClaimPath: "account_id", value: "not-a-jwt"}
	req, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-token")

	if injectRequest(req, sec) {
		t.Fatal("injectRequest allowed an invalid JWT")
	}
}

func TestInjectAlwaysPathInjectsWithoutPresenceOnClaimEntry(t *testing.T) {
	sec := &secretEntry{
		Header:            "Authorization",
		ValuePrefix:       "Bearer ",
		ClaimHeader:       "chatgpt-account-id",
		ClaimPath:         "account_id",
		InjectAlwaysPaths: []string{"/backend-api/ps/mcp"},
		value:             testJWT(`{"account_id":"real-account"}`),
	}
	req, err := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/ps/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	// No Authorization header at all: the rmcp connector client (issue #4298)
	// never sends one, so presence cannot signal intent here.

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest denied a listed path with no Authorization header")
	}
	if got := req.Header.Get("Authorization"); got != "Bearer "+sec.value {
		t.Errorf("Authorization = %q, want the injected credential", got)
	}
	if got := req.Header.Get("chatgpt-account-id"); got != "real-account" {
		t.Errorf("chatgpt-account-id = %q, want the extracted account ID", got)
	}
}

func TestInjectAlwaysPathDeniesAnUnlistedPathWithoutPresence(t *testing.T) {
	sec := &secretEntry{
		Header:            "Authorization",
		ValuePrefix:       "Bearer ",
		ClaimHeader:       "chatgpt-account-id",
		ClaimPath:         "account_id",
		InjectAlwaysPaths: []string{"/backend-api/ps/mcp"},
		value:             testJWT(`{"account_id":"real-account"}`),
	}
	req, err := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	// Same entry as above, but the request is not to a listed path, so it
	// still needs header presence, exactly like an entry with no list at all.

	if injectRequest(req, sec) {
		t.Fatal("injectRequest allowed an unlisted path with no Authorization header")
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want empty; an unlisted path must stay uncredentialed", got)
	}
}

func TestInjectAlwaysPathClaimFailureStillDenies(t *testing.T) {
	sec := &secretEntry{
		Header:            "Authorization",
		ClaimHeader:       "chatgpt-account-id",
		ClaimPath:         "account_id",
		InjectAlwaysPaths: []string{"/backend-api/ps/mcp"},
		value:             "not-a-jwt",
	}
	req, err := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/ps/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	// The path is listed, so the request IS requested, but claim resolution
	// still fails on a bad token: fail-closed must win regardless of which
	// signal produced "requested".

	if injectRequest(req, sec) {
		t.Fatal("injectRequest allowed a listed path whose claim resolution failed")
	}
	if got := req.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want empty on claim failure", got)
	}
}

func TestInjectAlwaysPathOnNonClaimEntry(t *testing.T) {
	sec := &secretEntry{
		Header:      "Authorization",
		ValuePrefix: "Bearer ",
		// The trailing "" pins the fail-open guard: an empty entry (a config
		// typo, a bare "-" list item rendering as null -> "") must never match,
		// or it would act as a wildcard for any path-less request.
		InjectAlwaysPaths: []string{"/backend-api/ps/mcp", ""},
		value:             "real-token",
	}
	tests := []struct {
		name, path string
		want       bool
	}{
		{"listed path, no header", "/backend-api/ps/mcp", true},
		{"unlisted path, no header", "/backend-api/other", false},
		// Matching is on req.URL.Path, which excludes the query string, so a
		// query on an otherwise-listed path must not change the outcome.
		{"listed path with query string, no header", "/backend-api/ps/mcp?session_id=x", true},
		// Exact match only: a trailing slash is a different path.
		{"trailing slash variant, no header", "/backend-api/ps/mcp/", false},
		// A path-less request (URL.Path == "") must not match the empty entry
		// in InjectAlwaysPaths above.
		{"path-less request, no header", "", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodPost, "https://chatgpt.com"+tt.path, nil)
			if err != nil {
				t.Fatal(err)
			}
			if got := injectRequest(req, sec); got != tt.want {
				t.Fatalf("injected = %v, want %v", got, tt.want)
			}
			if tt.want {
				if got := req.Header.Get("Authorization"); got != "Bearer real-token" {
					t.Errorf("Authorization = %q, want the injected credential", got)
				}
			} else if got := req.Header.Get("Authorization"); got != "" {
				t.Errorf("Authorization = %q, want empty", got)
			}
		})
	}
}

func TestInjectAlwaysPathDoesNotBreakPresenceOnUnlistedPath(t *testing.T) {
	// No regression check: an entry with InjectAlwaysPaths configured must
	// still honour ordinary presence-keyed injection on every other path.
	sec := &secretEntry{
		Header:            "Authorization",
		ValuePrefix:       "Bearer ",
		InjectAlwaysPaths: []string{"/backend-api/ps/mcp"},
		value:             "real-token",
	}
	req, err := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer guest-dummy")

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest denied a request that sent the header, on an entry with InjectAlwaysPaths configured")
	}
	if got := req.Header.Get("Authorization"); got != "Bearer real-token" {
		t.Errorf("Authorization = %q, want the injected credential", got)
	}
}

func TestInjectRequestBackwardCompatible(t *testing.T) {
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "real-token"}
	req, err := http.NewRequest(http.MethodGet, "https://api.example.com", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "guest-token")

	if !injectRequest(req, sec) {
		t.Fatal("injectRequest denied a legacy catalog entry")
	}
	if got := req.Header.Get("Authorization"); got != "Bearer real-token" {
		t.Errorf("Authorization = %q, want legacy credential injection", got)
	}
}

func testJWT(payload string) string {
	encode := func(value string) string {
		return base64.RawURLEncoding.EncodeToString([]byte(value))
	}
	return encode(`{"alg":"none"}`) + "." + encode(payload) + ".signature"
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
		{"entry has neither source", `[{"header":"Authorization","egressTo":["api.example.com"]}]`, true},
		{"entry has both sources", `[{"header":"Authorization","env":"TOK","brokerGrant":"codex-cluster","egressTo":["api.example.com"]}]`, true},
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

func TestTokenBrokerCachesAndRefreshes(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_, _ = io.WriteString(w, `{"access_token":"token-`+fmt.Sprint(calls)+`","expires_at":"`+time.Now().Add(2*time.Minute).UTC().Format(time.RFC3339)+`"}`)
	}))
	defer server.Close()
	b := newTokenBroker(server.URL)
	first, _, err := b.token("codex-cluster")
	if err != nil {
		t.Fatal(err)
	}
	second, _, err := b.token("codex-cluster")
	if err != nil || first != second || calls != 1 {
		t.Fatalf("cached token = %q, %q, calls = %d", first, second, calls)
	}
	b.state("codex-cluster").expiresAt = time.Now().Add(59 * time.Second)
	third, _, err := b.token("codex-cluster")
	if err != nil || third == first || calls != 2 {
		t.Fatalf("refreshed token = %q, calls = %d, err = %v", third, calls, err)
	}
}

func TestTokenBrokerSingleFlightPerGrant(t *testing.T) {
	var calls int
	started := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		close(started)
		<-release
		_, _ = io.WriteString(w, `{"access_token":"shared","expires_at":"`+time.Now().Add(time.Hour).UTC().Format(time.RFC3339)+`"}`)
	}))
	defer server.Close()
	b := newTokenBroker(server.URL)
	results := make(chan error, 10)
	for i := 0; i < 10; i++ {
		go func() {
			_, _, err := b.token("codex-cluster")
			results <- err
		}()
	}
	<-started
	close(release)
	for i := 0; i < 10; i++ {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	if calls != 1 {
		t.Fatalf("broker calls = %d, want 1", calls)
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
