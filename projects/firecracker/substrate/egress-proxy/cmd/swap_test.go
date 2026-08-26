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
		{"credential trailer", "POST / HTTP/1.1\r\nHost: api.example.com\r\nTransfer-Encoding: chunked\r\nTrailer: Authorization\r\n\r\n0\r\nAuthorization: Bearer evil\r\n\r\n"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			upClient, upOrigin := net.Pipe()
			defer upClient.Close()
			defer upOrigin.Close()
			sec := &secretEntry{Header: "Authorization", value: "real"}
			p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
			done := make(chan struct{})
			go func() {
				p.swapPump(bufio.NewReader(strings.NewReader(tt.request)), io.Discard, nil, upClient, "api.example.com", sec)
				close(done)
			}()
			_ = upOrigin.SetReadDeadline(time.Now().Add(20 * time.Millisecond))
			var b [1]byte
			if n, err := upOrigin.Read(b[:]); n != 0 || err == nil {
				t.Fatalf("unsupported request was forwarded: n=%d err=%v", n, err)
			}
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("swapPump did not reject the request promptly")
			}
		})
	}
}

func TestSwapPumpRejectsOversizeHeaders(t *testing.T) {
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	defer upOrigin.Close()
	request := "GET / HTTP/1.1\r\nHost: api.example.com\r\nX-Large: " + strings.Repeat("a", maxHeaderBytes) + "\r\n\r\n"
	sec := &secretEntry{Header: "Authorization", value: "real"}
	p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	p.swapPump(bufio.NewReader(strings.NewReader(request)), io.Discard, nil, upClient, "api.example.com", sec)

	_ = upOrigin.SetReadDeadline(time.Now().Add(20 * time.Millisecond))
	var b [1]byte
	if n, err := upOrigin.Read(b[:]); n != 0 || err == nil {
		t.Fatalf("oversize request was forwarded: n=%d err=%v", n, err)
	}
}

func TestSwapPumpHeaderDeadline(t *testing.T) {
	previousTimeout := headerTimeout
	headerTimeout = 50 * time.Millisecond
	t.Cleanup(func() { headerTimeout = previousTimeout })

	guestClient, guestProxy := net.Pipe()
	defer guestClient.Close()
	defer guestProxy.Close()
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	defer upOrigin.Close()
	done := make(chan struct{})
	go func() {
		sec := &secretEntry{Header: "Authorization", value: "real"}
		p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
		p.swapPump(bufio.NewReader(guestProxy), io.Discard, guestProxy, upClient, "api.example.com", sec)
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("swapPump did not return after the header deadline")
	}
}

func TestSwapPumpCanonicalizesHost(t *testing.T) {
	tests := []struct {
		name, request string
		wantForward   bool
	}{
		{
			name:        "origin form host with port and mixed case",
			request:     "GET /v1 HTTP/1.1\r\nHost: API.EXAMPLE.COM:443\r\nConnection: close\r\n\r\n",
			wantForward: true,
		},
		{
			name:        "absolute form",
			request:     "POST http://api.example.com/v1 HTTP/1.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
			wantForward: true,
		},
		{
			name:        "absolute form with matching host header",
			request:     "POST http://api.example.com/v1 HTTP/1.1\r\nHost: API.EXAMPLE.COM:443\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
			wantForward: true,
		},
		{
			name:        "origin form host with trailing dot",
			request:     "GET /v1 HTTP/1.1\r\nHost: api.example.com.\r\nConnection: close\r\n\r\n",
			wantForward: true,
		},
		{
			name:    "origin form mismatched host",
			request: "GET / HTTP/1.1\r\nHost: evil.example.com\r\nConnection: close\r\n\r\n",
		},
		{
			name:    "absolute form mismatched host",
			request: "GET http://evil.example.com/ HTTP/1.1\r\nConnection: close\r\n\r\n",
		},
		{
			name:    "absolute form with mismatched host header",
			request: "GET http://api.example.com/ HTTP/1.1\r\nHost: evil.example.com\r\nConnection: close\r\n\r\n",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			upClient, upOrigin := net.Pipe()
			defer upClient.Close()
			defer upOrigin.Close()
			type result struct {
				req *http.Request
				err error
			}
			seen := make(chan result, 1)
			go func() {
				_ = upOrigin.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
				req, err := http.ReadRequest(bufio.NewReader(upOrigin))
				seen <- result{req: req, err: err}
				if err == nil {
					_, _ = io.WriteString(upOrigin, "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
				}
			}()

			sec := &secretEntry{Header: "Authorization", value: "real"}
			p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
			p.swapPump(bufio.NewReader(strings.NewReader(tt.request)), io.Discard, nil, upClient, "api.example.com", sec)
			got := <-seen
			if !tt.wantForward {
				if got.err == nil {
					t.Fatalf("mismatched authority was forwarded: Host = %q", got.req.Host)
				}
				return
			}
			if got.err != nil {
				t.Fatalf("origin did not receive request: %v", got.err)
			}
			if got.req.Host != "api.example.com" {
				t.Fatalf("forwarded Host = %q, want api.example.com", got.req.Host)
			}
		})
	}
}

func TestSwapPumpKeepsBodyAttachedAcrossKeepAlive(t *testing.T) {
	guest := "POST /one HTTP/1.1\r\nHost: api.example.com\r\nContent-Length: 4\r\n\r\nbody" +
		"GET /two HTTP/1.1\r\nHost: api.example.com\r\nConnection: close\r\n\r\n"
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	defer upOrigin.Close()
	seen := make(chan []string, 1)
	go func() {
		upR := bufio.NewReader(upOrigin)
		var paths []string
		for i := 0; i < 2; i++ {
			req, err := http.ReadRequest(upR)
			if err != nil {
				seen <- paths
				return
			}
			body, err := io.ReadAll(req.Body)
			_ = req.Body.Close()
			if err != nil || (i == 0 && string(body) != "body") {
				seen <- paths
				return
			}
			paths = append(paths, req.URL.Path)
			_, _ = io.WriteString(upOrigin, "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
		}
		seen <- paths
	}()

	sec := &secretEntry{Header: "Authorization", value: "real"}
	p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	p.swapPump(bufio.NewReader(strings.NewReader(guest)), io.Discard, nil, upClient, "api.example.com", sec)
	paths := <-seen
	if len(paths) != 2 || paths[0] != "/one" || paths[1] != "/two" {
		t.Fatalf("forwarded paths = %v, want [/one /two]", paths)
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
		p.swapPump(bufio.NewReader(strings.NewReader(guest)), &back, nil, upClient, "api.example.com", sec)
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
	p.swapPump(bufio.NewReader(strings.NewReader(guest)), &back, nil, upClient, "api.example.com", sec)

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

func runSwapResponse(p *proxy, sec *secretEntry, status int, body string) (string, error) {
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	originErr := make(chan error, 1)
	go func() {
		defer upOrigin.Close()
		if _, err := http.ReadRequest(bufio.NewReader(upOrigin)); err != nil {
			originErr <- err
			return
		}
		_, err := fmt.Fprintf(upOrigin, "HTTP/1.1 %d %s\r\nX-Upstream: preserved\r\nContent-Length: %d\r\n\r\n%s", status, http.StatusText(status), len(body), body)
		originErr <- err
	}()
	guest := "POST http://api.example.com/v1/messages HTTP/1.1\r\n" +
		"Host: api.example.com\r\n" +
		"Authorization: Bearer guest-login-gate-dummy\r\n" +
		"Content-Length: 0\r\n" +
		"Connection: close\r\n\r\n"
	var back bytes.Buffer
	p.swapPump(bufio.NewReader(strings.NewReader(guest)), &back, nil, upClient, "api.example.com", sec)
	return back.String(), <-originErr
}

func TestSwapPumpUnauthorizedInvalidatesBrokerCredentialAndForcesRefreshOnce(t *testing.T) {
	var countMu sync.Mutex
	refreshes := 0
	refreshed := make(chan struct{}, 1)
	brokerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/grants/codex-cluster/refresh" {
			t.Errorf("refresh request = %s %s", r.Method, r.URL.Path)
		}
		countMu.Lock()
		refreshes++
		countMu.Unlock()
		select {
		case refreshed <- struct{}{}:
		default:
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer brokerServer.Close()

	b := newTokenBroker(brokerServer.URL)
	bState := b.state("codex-cluster")
	bState.token, bState.expiresAt = "dead-token", time.Now().Add(time.Hour)
	sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", BrokerGrant: "codex-cluster", value: "dead-token", expiresAt: time.Now().Add(time.Hour), broker: b, mu: &sync.RWMutex{}}
	p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

	const callers = 6
	responses := make([]string, callers)
	errs := make([]error, callers)
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			responses[index], errs[index] = runSwapResponse(p, sec, http.StatusUnauthorized, "token expired")
		}(i)
	}
	wg.Wait()
	select {
	case <-refreshed:
	case <-time.After(time.Second):
		t.Fatal("broker did not receive force refresh")
	}
	time.Sleep(50 * time.Millisecond)

	countMu.Lock()
	gotRefreshes := refreshes
	countMu.Unlock()
	if gotRefreshes != 1 {
		t.Fatalf("refresh POSTs = %d, want 1", gotRefreshes)
	}
	for i, err := range errs {
		if err != nil {
			t.Fatalf("caller %d origin error: %v", i, err)
		}
		resp, err := http.ReadResponse(bufio.NewReader(strings.NewReader(responses[i])), nil)
		if err != nil {
			t.Fatalf("caller %d response parse: %v", i, err)
		}
		body, _ := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusUnauthorized || resp.Header.Get("X-Upstream") != "preserved" || string(body) != "token expired" {
			t.Fatalf("caller %d response = %d %q %#v", i, resp.StatusCode, body, resp.Header)
		}
	}
	bState.mu.Lock()
	brokerToken, brokerExpiry := bState.token, bState.expiresAt
	bState.mu.Unlock()
	if brokerToken != "" || !brokerExpiry.IsZero() {
		t.Fatalf("broker cache = %q %v, want empty", brokerToken, brokerExpiry)
	}
	if sec.live() {
		t.Fatal("secret entry remained live after upstream 401")
	}
}

func TestSwapPumpRefreshesOnlyBrokerCredentialsOnUnauthorized(t *testing.T) {
	tests := []struct {
		name        string
		status      int
		brokerGrant string
	}{
		{name: "forbidden broker credential", status: http.StatusForbidden, brokerGrant: "codex-cluster"},
		{name: "unauthorized environment credential", status: http.StatusUnauthorized},
		{name: "successful broker credential", status: http.StatusOK, brokerGrant: "codex-cluster"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var countMu sync.Mutex
			refreshes := 0
			brokerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				countMu.Lock()
				refreshes++
				countMu.Unlock()
				w.WriteHeader(http.StatusOK)
			}))
			defer brokerServer.Close()
			b := newTokenBroker(brokerServer.URL)
			sec := &secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", Env: "TOKEN", BrokerGrant: tt.brokerGrant, value: "credential", expiresAt: time.Now().Add(time.Hour), broker: b, mu: &sync.RWMutex{}}
			p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil))}

			response, err := runSwapResponse(p, sec, tt.status, "upstream response")
			if err != nil {
				t.Fatal(err)
			}
			parsed, err := http.ReadResponse(bufio.NewReader(strings.NewReader(response)), nil)
			if err != nil {
				t.Fatal(err)
			}
			_ = parsed.Body.Close()
			if parsed.StatusCode != tt.status {
				t.Fatalf("guest status = %d, want %d", parsed.StatusCode, tt.status)
			}
			time.Sleep(50 * time.Millisecond)
			countMu.Lock()
			gotRefreshes := refreshes
			countMu.Unlock()
			if gotRefreshes != 0 {
				t.Fatalf("refresh POSTs = %d, want 0", gotRefreshes)
			}
			if !sec.live() {
				t.Fatal("credential was invalidated outside broker-backed 401 handling")
			}
		})
	}
}

// git over HTTPS and the GitHub API want the SAME token in two shapes. Verified
// against github.com with a real PAT: git-receive-pack 401s on Bearer and 200s
// on Basic, while api.github.com 200s on Bearer. Encoding in the sidecar keeps
// ONE credential in the vault instead of a derived base64 copy that would
// silently outlive a rotation.
func TestHeaderValueEncodesBasicWhenUserIsSet(t *testing.T) {
	basic := secretEntry{Header: "Authorization", BasicUser: "x-access-token", value: "ghp_example"}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:ghp_example"))
	if got := basic.headerValue(); got != want {
		t.Errorf("headerValue() = %q, want %q", got, want)
	}

	bearer := secretEntry{Header: "Authorization", ValuePrefix: "Bearer ", value: "ghp_example"}
	if got, want := bearer.headerValue(), "Bearer ghp_example"; got != want {
		t.Errorf("headerValue() = %q, want %q", got, want)
	}

	// Same secret, two lanes, two shapes: this is the whole point.
	if basic.headerValue() == bearer.headerValue() {
		t.Error("basic and bearer encodings of the same token must differ")
	}
}

func TestLoadSecretsRejectsBothBasicUserAndValuePrefix(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	called := 0
	prev := exitFn
	exitFn = func(int) { called++ }
	defer func() { exitFn = prev }()

	t.Setenv("EGRESS_SECRETS", `[{"header":"Authorization","valuePrefix":"Bearer ","basicUser":"x-access-token","env":"TOK","egressTo":["github.com"]}]`)
	loadSecrets(logger)
	if called == 0 {
		t.Error("an entry setting both encodings must refuse to start, not pick one")
	}
}
