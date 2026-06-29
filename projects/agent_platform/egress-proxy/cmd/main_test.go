package main

import (
	"bufio"
	"bytes"
	"crypto/tls"
	"io"
	"net"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestAllowed(t *testing.T) {
	tests := []struct {
		name      string
		dest      string
		allowlist []string
		want      bool
	}{
		{
			name:      "exact host matches any port",
			dest:      "qwen:8000",
			allowlist: []string{"qwen"},
			want:      true,
		},
		{
			name:      "exact host matches a different port too",
			dest:      "qwen:9999",
			allowlist: []string{"qwen"},
			want:      true,
		},
		{
			name:      "exact host:port match",
			dest:      "qwen:8000",
			allowlist: []string{"qwen:8000"},
			want:      true,
		},
		{
			name:      "port mismatch denied",
			dest:      "qwen:8001",
			allowlist: []string{"qwen:8000"},
			want:      false,
		},
		{
			name:      "host mismatch denied",
			dest:      "evil:8000",
			allowlist: []string{"qwen:8000"},
			want:      false,
		},
		{
			name:      "empty allowlist denies everything",
			dest:      "qwen:8000",
			allowlist: nil,
			want:      false,
		},
		{
			name:      "allowlist of only empty entries denies",
			dest:      "qwen:8000",
			allowlist: []string{"", "   "},
			want:      false,
		},
		{
			name:      "case-insensitive dest host",
			dest:      "QWEN:8000",
			allowlist: []string{"qwen"},
			want:      true,
		},
		{
			name:      "case-insensitive allowlist host",
			dest:      "qwen:8000",
			allowlist: []string{"QWEN:8000"},
			want:      true,
		},
		{
			name:      "no suffix matching (prefix attacker host)",
			dest:      "evil-qwen:8000",
			allowlist: []string{"qwen"},
			want:      false,
		},
		{
			name:      "no suffix matching (subdomain not parent)",
			dest:      "qwen.example.com:443",
			allowlist: []string{"example.com"},
			want:      false,
		},
		{
			name:      "dest without port, entry without port",
			dest:      "qwen",
			allowlist: []string{"qwen"},
			want:      true,
		},
		{
			name:      "dest without port denied when entry requires a port",
			dest:      "qwen",
			allowlist: []string{"qwen:8000"},
			want:      false,
		},
		{
			name:      "matches when one of several entries matches",
			dest:      "context-forge:4444",
			allowlist: []string{"qwen:8000", "context-forge:4444", "registry"},
			want:      true,
		},
		{
			name:      "denied when no entry of several matches",
			dest:      "exfil.example.com:443",
			allowlist: []string{"qwen:8000", "context-forge:4444"},
			want:      false,
		},
		{
			name:      "whitespace around entries is tolerated",
			dest:      "qwen:8000",
			allowlist: []string{"  qwen:8000  "},
			want:      true,
		},
		{
			name:      "fully-qualified service host matches",
			dest:      "qwen.upstream.example:8000",
			allowlist: []string{"qwen.upstream.example"},
			want:      true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := allowed(tt.dest, tt.allowlist); got != tt.want {
				t.Errorf("allowed(%q, %v) = %v, want %v", tt.dest, tt.allowlist, got, tt.want)
			}
		})
	}
}

func TestParseAllowlist(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []string
	}{
		{
			name: "empty string yields empty list",
			in:   "",
			want: []string{},
		},
		{
			name: "single entry",
			in:   "qwen:8000",
			want: []string{"qwen:8000"},
		},
		{
			name: "multiple entries trimmed",
			in:   "qwen:8000, context-forge:4444 ,registry",
			want: []string{"qwen:8000", "context-forge:4444", "registry"},
		},
		{
			name: "empty entries dropped",
			in:   "qwen:8000,,  ,context-forge:4444",
			want: []string{"qwen:8000", "context-forge:4444"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := parseAllowlist(tt.in)
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("parseAllowlist(%q) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}

func TestHostFromHTTP(t *testing.T) {
	tests := []struct {
		name string
		req  string
		want string
	}{
		{
			name: "host with port stripped",
			req:  "POST /v1/chat/completions HTTP/1.1\r\nHost: model.example.com:8080\r\nContent-Type: application/json\r\n\r\n{}",
			want: "model.example.com",
		},
		{
			name: "host without port",
			req:  "GET / HTTP/1.1\r\nHost: api.github.com\r\n\r\n",
			want: "api.github.com",
		},
		{
			name: "host header case-insensitive and whitespace tolerant",
			req:  "GET / HTTP/1.1\r\nhOsT:   example.com  \r\n\r\n",
			want: "example.com",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			br := bufio.NewReader(strings.NewReader(tt.req))
			got, isTLS, err := hostFromStream(br)
			if err != nil {
				t.Fatalf("hostFromStream: %v", err)
			}
			if isTLS {
				t.Errorf("plain HTTP misdetected as TLS")
			}
			if got != tt.want {
				t.Errorf("host = %q, want %q", got, tt.want)
			}
			// Peek must not consume: the request still forwards verbatim.
			rest, _ := io.ReadAll(br)
			if !strings.HasPrefix(string(rest), "POST ") && !strings.HasPrefix(string(rest), "GET ") {
				t.Errorf("stream was consumed by peek; remainder=%q", rest)
			}
		})
	}
}

func TestHostFromHTTPNoHost(t *testing.T) {
	br := bufio.NewReader(strings.NewReader("GET / HTTP/1.1\r\nAccept: */*\r\n\r\n"))
	if _, _, err := hostFromStream(br); err == nil {
		t.Fatal("expected error for missing Host header")
	}
}

func TestSNIFromClientHello(t *testing.T) {
	const name = "api.github.com"
	hello := captureClientHello(t, name)
	if hello[0] != 0x16 {
		t.Fatalf("captured bytes are not a TLS handshake record: 0x%02x", hello[0])
	}

	// Direct parse.
	got, err := parseSNI(hello)
	if err != nil {
		t.Fatalf("parseSNI: %v", err)
	}
	if got != name {
		t.Errorf("parseSNI = %q, want %q", got, name)
	}

	// Via the peeking dispatcher, which must not consume the ClientHello.
	br := bufio.NewReader(bytes.NewReader(hello))
	got, isTLS, err := hostFromStream(br)
	if err != nil {
		t.Fatalf("hostFromStream(TLS): %v", err)
	}
	if !isTLS {
		t.Errorf("TLS ClientHello not detected as TLS")
	}
	if got != name {
		t.Errorf("hostFromStream = %q, want %q", got, name)
	}
	rest, _ := io.ReadAll(br)
	if len(rest) != len(hello) {
		t.Errorf("ClientHello partly consumed: %d of %d bytes remain", len(rest), len(hello))
	}
}

func TestParseSNITruncated(t *testing.T) {
	if _, err := parseSNI([]byte{0x16, 0x03, 0x01, 0x00, 0x05, 0x01}); err == nil {
		t.Fatal("expected error on truncated ClientHello")
	}
}

// captureClientHello drives crypto/tls to emit a real ClientHello for name and
// returns the raw record bytes (the handshake fails after the write, as intended).
func captureClientHello(t *testing.T, name string) []byte {
	t.Helper()
	var buf bytes.Buffer
	c := &captureConn{w: &buf}
	tlsConn := tls.Client(c, &tls.Config{ServerName: name, InsecureSkipVerify: true}) //nolint:gosec // test-only
	_ = tlsConn.Handshake()                                                           // errors after writing ClientHello; bytes are captured
	if buf.Len() == 0 {
		t.Fatal("no ClientHello captured")
	}
	return buf.Bytes()
}

// captureConn is a net.Conn that records writes and fails reads, so a TLS client
// writes its ClientHello and then aborts.
type captureConn struct{ w io.Writer }

func (c *captureConn) Write(b []byte) (int, error)      { return c.w.Write(b) }
func (c *captureConn) Read([]byte) (int, error)         { return 0, io.EOF }
func (c *captureConn) Close() error                     { return nil }
func (c *captureConn) LocalAddr() net.Addr              { return nil }
func (c *captureConn) RemoteAddr() net.Addr             { return nil }
func (c *captureConn) SetDeadline(time.Time) error      { return nil }
func (c *captureConn) SetReadDeadline(time.Time) error  { return nil }
func (c *captureConn) SetWriteDeadline(time.Time) error { return nil }

func TestSplitHostPort(t *testing.T) {
	tests := []struct {
		in       string
		wantHost string
		wantPort string
	}{
		{in: "qwen:8000", wantHost: "qwen", wantPort: "8000"},
		{in: "qwen", wantHost: "qwen", wantPort: ""},
		{in: "example.com:443", wantHost: "example.com", wantPort: "443"},
		{in: "", wantHost: "", wantPort: ""},
	}

	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			host, port := splitHostPort(tt.in)
			if host != tt.wantHost || port != tt.wantPort {
				t.Errorf("splitHostPort(%q) = (%q, %q), want (%q, %q)", tt.in, host, port, tt.wantHost, tt.wantPort)
			}
		})
	}
}

// blockAfterReader yields its payload on the first Read, then blocks until
// released, reproducing a real socket where the client has sent its whole
// request head and is waiting for the response (no EOF). A fixed-size Peek
// deadlocks on this; the buffered-growth scan must not.
type blockAfterReader struct {
	data    []byte
	served  bool
	release chan struct{}
}

func (r *blockAfterReader) Read(p []byte) (int, error) {
	if !r.served {
		r.served = true
		return copy(p, r.data), nil
	}
	<-r.release
	return 0, io.EOF
}

func TestHostFromHTTPSmallBodylessGetDoesNotHang(t *testing.T) {
	// A bodyless GET shorter than the old fixed Peek(1024): the whole head is sent,
	// then the client waits. hostFromHTTP must find the Host and return, not block.
	req := "GET /internal/artifact/123/session HTTP/1.1\r\n" +
		"Host: monolith.test.example:8000\r\n" +
		"User-Agent: Go-http-client/1.1\r\n\r\n"
	r := &blockAfterReader{data: []byte(req), release: make(chan struct{})}
	defer close(r.release)
	br := bufio.NewReaderSize(r, maxHeadPeek)

	type result struct {
		host string
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		h, err := hostFromHTTP(br)
		ch <- result{h, err}
	}()
	select {
	case got := <-ch:
		if got.err != nil {
			t.Fatalf("hostFromHTTP: %v", got.err)
		}
		if got.host != "monolith.test.example" {
			t.Fatalf("host = %q, want monolith.test.example", got.host)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("hostFromHTTP hung on a small bodyless GET (Peek-blocks-for-fixed-n regression)")
	}
}
