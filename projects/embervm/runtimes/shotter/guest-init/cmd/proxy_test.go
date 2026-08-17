package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/runtimes/shotter"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

const (
	publicHost      = "jomcgi.dev"
	internalService = "monolith-public-frontend.monolith-public.svc.cluster.local:3000"

	// privateHost is the second entry the real baked shotter-egress.json
	// carries (ADR embervm/035 section 3): it is the one that reaches the
	// private tier, so it needs its own dial-through coverage, not just the
	// public jomcgi.dev mapping that allowedProxyConfig exercises everywhere
	// else in this file.
	privateHost            = "private.jomcgi.dev"
	privateInternalService = "monolith.monolith.svc.cluster.local:3000"
)

func allowedProxyConfig() ProxyConfig {
	return ProxyConfig{
		HostMapping: map[string]string{publicHost: internalService},
		Allowlist:   []string{internalService},
	}
}

// bothMappingsProxyConfig carries both entries the real baked
// shotter-egress.json does, for tests that specifically need the second
// (private.jomcgi.dev) mapping alongside the first.
func bothMappingsProxyConfig() ProxyConfig {
	return ProxyConfig{
		HostMapping: map[string]string{
			publicHost:  internalService,
			privateHost: privateInternalService,
		},
		Allowlist: []string{internalService, privateInternalService},
	}
}

type dialCall struct {
	cid  uint32
	port uint32
}

type recordingDialer struct {
	mu       sync.Mutex
	calls    []dialCall
	upstream func(net.Conn)
}

func (d *recordingDialer) dial(_ context.Context, cid, port uint32) (net.Conn, error) {
	d.mu.Lock()
	d.calls = append(d.calls, dialCall{cid: cid, port: port})
	d.mu.Unlock()

	proxySide, testSide := net.Pipe()
	go d.upstream(testSide)
	return proxySide, nil
}

func (d *recordingDialer) callCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.calls)
}

func (d *recordingDialer) recordedCalls() []dialCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]dialCall(nil), d.calls...)
}

func runProxyRequest(t *testing.T, server *ProxyServer, request []byte) {
	t.Helper()
	client, proxy := net.Pipe()
	done := make(chan struct{})
	go func() {
		server.handleConnection(proxy)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	if err := client.SetDeadline(deadline); err != nil {
		t.Fatalf("SetDeadline: %v", err)
	}
	if _, err := client.Write(request); err != nil {
		t.Fatalf("write request: %v", err)
	}
	_, readErr := io.Copy(io.Discard, client)
	if readErr != nil {
		t.Fatalf("wait for proxy close: %v", readErr)
	}
	if err := client.Close(); err != nil {
		t.Fatalf("close client: %v", err)
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("proxy handler did not stop")
	}
}

func refusalDialer(t *testing.T) *recordingDialer {
	t.Helper()
	return &recordingDialer{
		upstream: func(conn net.Conn) {
			_ = conn.Close()
		},
	}
}

func assertRefused(t *testing.T, config ProxyConfig, request string) {
	t.Helper()
	dialer := refusalDialer(t)
	server := newProxyServer(config, nil, dialer.dial)
	runProxyRequest(t, server, []byte(request))
	if got := dialer.callCount(); got != 0 {
		t.Fatalf("vsock dial count = %d, want 0", got)
	}
}

func TestProxyMappedHostDialsInternalServiceAndPreservesRequest(t *testing.T) {
	request := []byte("GET http://jomcgi.dev/path HTTP/1.1\r\nHost: jomcgi.dev\r\nUser-Agent: shotter-test\r\n\r\n")
	upstreamBytes := make(chan []byte, 1)
	dialer := &recordingDialer{
		upstream: func(conn net.Conn) {
			defer conn.Close()
			reader := bufio.NewReader(conn)
			preamble, err := reader.ReadString('\n')
			if err != nil {
				t.Errorf("read preamble: %v", err)
				return
			}
			replayed := make([]byte, len(request))
			if _, err := io.ReadFull(reader, replayed); err != nil {
				t.Errorf("read replayed request: %v", err)
				return
			}
			upstreamBytes <- append([]byte(preamble), replayed...)
		},
	}

	server := newProxyServer(allowedProxyConfig(), nil, dialer.dial)
	runProxyRequest(t, server, request)

	calls := dialer.recordedCalls()
	if len(calls) != 1 {
		t.Fatalf("vsock dial count = %d, want 1", len(calls))
	}
	if calls[0] != (dialCall{cid: vsockproto.HostCID, port: vsockproto.EgressPort}) {
		t.Fatalf("vsock dial = %+v, want CID %d port %d", calls[0], vsockproto.HostCID, vsockproto.EgressPort)
	}
	wantUpstream := append([]byte(internalService+"\n"), request...)
	select {
	case got := <-upstreamBytes:
		if string(got) != string(wantUpstream) {
			t.Fatalf("upstream bytes = %q, want %q", got, wantUpstream)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not receive preamble and request")
	}
}

func TestProxyRefusesAPIGitHubWithoutDial(t *testing.T) {
	assertRefused(t, allowedProxyConfig(), "GET http://api.github.com/user HTTP/1.1\r\nHost: api.github.com\r\n\r\n")
}

func TestProxyRefusesMappedHostLookalikesWithoutDial(t *testing.T) {
	tests := []struct {
		name    string
		request string
	}{
		{
			name:    "hyphen prefix",
			request: "CONNECT evil-jomcgi.dev:443 HTTP/1.1\r\nHost: evil-jomcgi.dev:443\r\n\r\n",
		},
		{
			name:    "longer suffix",
			request: "GET http://jomcgi.dev.evil.com/ HTTP/1.1\r\nHost: jomcgi.dev.evil.com\r\n\r\n",
		},
		{
			name:    "different subdomain",
			request: "GET http://staging.jomcgi.dev/ HTTP/1.1\r\nHost: staging.jomcgi.dev\r\n\r\n",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			assertRefused(t, allowedProxyConfig(), test.request)
		})
	}
}

func TestProxyRefusesUnmappedSubresourceAfterMappedTopLevel(t *testing.T) {
	request := []byte("GET http://jomcgi.dev/path HTTP/1.1\r\nHost: jomcgi.dev\r\n\r\n")
	dialer := &recordingDialer{
		upstream: func(conn net.Conn) {
			defer conn.Close()
			reader := bufio.NewReader(conn)
			if _, err := reader.ReadString('\n'); err != nil {
				t.Errorf("read top-level preamble: %v", err)
				return
			}
			replayed := make([]byte, len(request))
			if _, err := io.ReadFull(reader, replayed); err != nil {
				t.Errorf("read top-level request: %v", err)
			}
		},
	}
	server := newProxyServer(allowedProxyConfig(), nil, dialer.dial)
	runProxyRequest(t, server, request)
	if got := dialer.callCount(); got != 1 {
		t.Fatalf("top-level vsock dial count = %d, want 1", got)
	}

	subresource := "GET http://api.anthropic.com/v1/models HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n"
	runProxyRequest(t, server, []byte(subresource))
	if got := dialer.callCount(); got != 1 {
		t.Fatalf("vsock dial count after refused subresource = %d, want 1", got)
	}
}

func TestProxyRefusesUnmappedConnectWithoutDial(t *testing.T) {
	assertRefused(t, allowedProxyConfig(), "CONNECT evil-api.example.com:443 HTTP/1.1\r\nHost: evil-api.example.com:443\r\n\r\n")
}

// TestProxyRefusesCONNECTForMappedHostWithoutDial guards the fix for a
// mapped host reached via CONNECT rather than a plain absolute-form GET.
// Every mapped host resolves to a plaintext internal destination, so
// tunnelling a CONNECT for one (the shape a page's own absolute https://
// subresource produces) can only ever end in an opaque TLS failure inside
// the capture. This must be refused up front, the same way an unmapped
// CONNECT already is, not admitted and left to fail deep in a handshake.
func TestProxyRefusesCONNECTForMappedHostWithoutDial(t *testing.T) {
	assertRefused(t, allowedProxyConfig(), fmt.Sprintf("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n", publicHost, publicHost))
}

// TestProxyMappedHostDialsInternalServiceAndPreservesRequestForPrivateHost
// covers the second baked mapping. jomcgi.dev's dial-through is asserted in
// TestProxyMappedHostDialsInternalServiceAndPreservesRequest above;
// private.jomcgi.dev is the one that actually reaches the private tier
// behind Cloudflare Access, and previously had no dedicated coverage.
func TestProxyMappedHostDialsInternalServiceAndPreservesRequestForPrivateHost(t *testing.T) {
	request := []byte(fmt.Sprintf("GET http://%s/agents HTTP/1.1\r\nHost: %s\r\nUser-Agent: shotter-test\r\n\r\n", privateHost, privateHost))
	upstreamBytes := make(chan []byte, 1)
	dialer := &recordingDialer{
		upstream: func(conn net.Conn) {
			defer conn.Close()
			reader := bufio.NewReader(conn)
			preamble, err := reader.ReadString('\n')
			if err != nil {
				t.Errorf("read preamble: %v", err)
				return
			}
			replayed := make([]byte, len(request))
			if _, err := io.ReadFull(reader, replayed); err != nil {
				t.Errorf("read replayed request: %v", err)
				return
			}
			upstreamBytes <- append([]byte(preamble), replayed...)
		},
	}

	server := newProxyServer(bothMappingsProxyConfig(), nil, dialer.dial)
	runProxyRequest(t, server, request)

	calls := dialer.recordedCalls()
	if len(calls) != 1 {
		t.Fatalf("vsock dial count = %d, want 1", len(calls))
	}
	wantUpstream := append([]byte(privateInternalService+"\n"), request...)
	select {
	case got := <-upstreamBytes:
		if string(got) != string(wantUpstream) {
			t.Fatalf("upstream bytes = %q, want %q", got, wantUpstream)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("upstream did not receive preamble and request")
	}
}

// TestLoadProxyConfigFromPathParsesBakedFileShape guards the on-disk schema
// that BUILD bakes into /etc/shotter-egress.json: a "mapping" object plus an
// "allowlist" array, the same shape as the checked-in
// projects/embervm/runtimes/shotter/etc/shotter-egress.json.
func TestLoadProxyConfigFromPathParsesBakedFileShape(t *testing.T) {
	path := filepath.Join(t.TempDir(), "shotter-egress.json")
	contents := fmt.Sprintf(`{"mapping":{%q:%q},"allowlist":[%q]}`, publicHost, internalService, internalService)
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		t.Fatalf("write config file: %v", err)
	}

	config, err := loadProxyConfigFromPath(path)
	if err != nil {
		t.Fatalf("loadProxyConfigFromPath: %v", err)
	}
	want := allowedProxyConfig()
	if len(config.HostMapping) != len(want.HostMapping) || config.HostMapping[publicHost] != want.HostMapping[publicHost] {
		t.Fatalf("HostMapping = %+v, want %+v", config.HostMapping, want.HostMapping)
	}
	if len(config.Allowlist) != 1 || config.Allowlist[0] != internalService {
		t.Fatalf("Allowlist = %+v, want [%q]", config.Allowlist, internalService)
	}

	assertDial := &recordingDialer{upstream: func(conn net.Conn) { _ = conn.Close() }}
	server := newProxyServer(config, nil, assertDial.dial)
	runProxyRequest(t, server, []byte("GET http://jomcgi.dev/path HTTP/1.1\r\nHost: jomcgi.dev\r\n\r\n"))
	if got := assertDial.callCount(); got != 1 {
		t.Fatalf("vsock dial count = %d, want 1", got)
	}
}

func TestProxyConfigurationFailuresRefuseAllWithoutDial(t *testing.T) {
	tests := []struct {
		name string
		// fileContents nil means the config file is never written, exercising
		// the absent-file path. A non-nil value is written verbatim.
		fileContents *string
	}{
		{name: "config file absent", fileContents: nil},
		{name: "config file is not valid JSON", fileContents: stringPointer("{")},
		{
			name: "mapping destination invalid",
			fileContents: stringPointer(fmt.Sprintf(
				`{"mapping":{%q:"not a valid destination"},"allowlist":[%q]}`,
				publicHost, internalService,
			)),
		},
		{
			name: "allowlist destination invalid",
			fileContents: stringPointer(fmt.Sprintf(
				`{"mapping":{%q:%q},"allowlist":["not a valid destination"]}`,
				publicHost, internalService,
			)),
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "shotter-egress.json")
			if test.fileContents != nil {
				if err := os.WriteFile(path, []byte(*test.fileContents), 0o644); err != nil {
					t.Fatalf("write config file: %v", err)
				}
			}
			config, err := loadProxyConfigFromPath(path)
			if err == nil {
				t.Fatal("loadProxyConfigFromPath succeeded, want fail-closed error")
			}
			assertRefused(t, config, "GET http://jomcgi.dev/path HTTP/1.1\r\nHost: jomcgi.dev\r\n\r\n")
		})
	}
}

func stringPointer(value string) *string {
	return &value
}

// TestBakedShotterEgressConfigParsesAndValidates gives the real, checked-in
// etc/shotter-egress.json (embedded via the sibling shotter package, not a
// synthetic fixture this file writes itself) a data dependency on this test.
// Every other case in this file exercises loadProxyConfigFromPath against
// JSON this file constructs, which proves the parser and validator work, not
// that the file BUILD actually bakes into the guest image is one of the
// things they accept. A malformed real file would otherwise be green in CI
// forever.
func TestBakedShotterEgressConfigParsesAndValidates(t *testing.T) {
	path := filepath.Join(t.TempDir(), "shotter-egress.json")
	if err := os.WriteFile(path, shotter.BakedEgressConfigJSON, 0o644); err != nil {
		t.Fatalf("write baked config: %v", err)
	}

	config, err := loadProxyConfigFromPath(path)
	if err != nil {
		t.Fatalf("loadProxyConfigFromPath(baked shotter-egress.json) = %v, want it to parse and validate", err)
	}
	if len(config.HostMapping) == 0 {
		t.Fatal("baked config HostMapping is empty")
	}
	if len(config.Allowlist) == 0 {
		t.Fatal("baked config Allowlist is empty")
	}

	// Anchor the synthetic constants to the real file. screenshot.go now
	// rewrites the navigate URL to the mapped destination, so the value below
	// is what Chromium actually dials. A non-empty check alone would let the
	// baked file drift away from these constants while every test that asserts
	// an exact internal hostname stayed green, and the drift would surface only
	// as a prod navigation to a stale service.
	wantMapping := map[string]string{
		publicHost:  internalService,
		privateHost: privateInternalService,
	}
	if !reflect.DeepEqual(config.HostMapping, wantMapping) {
		t.Fatalf("baked config HostMapping = %v, want %v", config.HostMapping, wantMapping)
	}
}
