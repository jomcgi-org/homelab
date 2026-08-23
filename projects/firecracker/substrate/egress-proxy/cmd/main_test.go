package main

import (
	"bufio"
	"bytes"
	"io"
	"log/slog"
	"net"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestOriginFormReader(t *testing.T) {
	absolute := "POST http://inference.inference.svc.cluster.local:8080/v1/chat/completions HTTP/1.1\r\n" +
		"Host: inference.inference.svc.cluster.local:8080\r\nContent-Length: 0\r\n\r\n"
	origin := "POST /v1/chat/completions HTTP/1.1\r\nHost: inference.inference.svc.cluster.local:8080\r\nContent-Length: 0\r\n\r\n"
	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "absolute form preserves host",
			in:   absolute,
			want: origin,
		},
		{
			name: "origin form passes through",
			in:   origin,
			want: origin,
		},
		{
			name: "keep alive rewrites every request",
			in:   absolute + absolute,
			want: origin + origin,
		},
		{
			name: "non HTTP bytes pass through",
			in:   "git-upload-pack\x00\x01\xffbinary",
			want: "git-upload-pack\x00\x01\xffbinary",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var got bytes.Buffer
			if _, err := io.Copy(&got, newOriginFormReader(bufio.NewReader(strings.NewReader(tt.in)))); err != nil {
				t.Fatal(err)
			}
			if got.String() != tt.want {
				t.Errorf("rewritten stream = %q, want %q", got.String(), tt.want)
			}
		})
	}
}

func TestOriginFormReaderHandlesSplitRequestLine(t *testing.T) {
	line := "POST http://inference.inference.svc.cluster.local:8080/v1/chat/completions HTTP/1.1\r\n"
	in := strings.NewReader(line[:17])
	reader := &chunkedReader{first: in, second: strings.NewReader(line[17:] + "Host: inference.inference.svc.cluster.local:8080\r\n\r\n")}
	var got bytes.Buffer
	if _, err := io.Copy(&got, newOriginFormReader(bufio.NewReader(reader))); err != nil {
		t.Fatal(err)
	}
	want := "POST /v1/chat/completions HTTP/1.1\r\nHost: inference.inference.svc.cluster.local:8080\r\n\r\n"
	if got.String() != want {
		t.Errorf("rewritten split stream = %q, want %q", got.String(), want)
	}
}

type chunkedReader struct {
	first, second io.Reader
}

func (r *chunkedReader) Read(p []byte) (int, error) {
	if r.first != nil {
		n, err := r.first.Read(p)
		if err == io.EOF {
			r.first = nil
		}
		if n > 0 {
			return n, nil
		}
	}
	return r.second.Read(p)
}

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

func TestDefaultListenAddrIsLoopback(t *testing.T) {
	t.Setenv("EGRESS_LISTEN", "")
	if got := envOr("EGRESS_LISTEN", defaultListenAddr); got != "127.0.0.1:8888" {
		t.Fatalf("default listen address = %q, want 127.0.0.1:8888", got)
	}
}

func TestHandleBoundsPreamble(t *testing.T) {
	previousTimeout := handshakeTimeout
	handshakeTimeout = 50 * time.Millisecond
	t.Cleanup(func() { handshakeTimeout = previousTimeout })

	tests := []struct {
		name  string
		input string
	}{
		{name: "oversize preamble without newline", input: strings.Repeat("a", maxPreambleBytes+1)},
		{name: "slow preamble"},
		{name: "valid denied internal destination", input: "internal.example:443\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			p := &proxy{
				lookupIP: func(string) ([]net.IP, error) {
					return []net.IP{net.ParseIP("127.0.0.1")}, nil
				},
				logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
			}
			go p.handle(server)
			if tt.input != "" {
				_, _ = io.WriteString(client, tt.input)
			}

			done := make(chan error, 1)
			go func() {
				var b [1]byte
				_, err := client.Read(b[:])
				done <- err
			}()
			select {
			case err := <-done:
				if err != io.EOF {
					t.Fatalf("client read error = %v, want EOF", err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("handle did not close the connection within the bound")
			}
		})
	}
}

func TestConnectionCap(t *testing.T) {
	p := &proxy{conns: make(chan struct{}, 2)}
	if !p.acquireConn() || !p.acquireConn() {
		t.Fatal("first two connections should fit under the cap")
	}
	if p.acquireConn() {
		t.Fatal("third connection should be rejected")
	}
	if got := p.rejectedConns.Load(); got != 1 {
		t.Fatalf("rejected connections = %d, want 1", got)
	}
	p.releaseConn()
	if !p.acquireConn() {
		t.Fatal("connection should be accepted after a release")
	}

	unlimited := &proxy{}
	for i := 0; i < 3; i++ {
		if !unlimited.acquireConn() {
			t.Fatal("nil connection semaphore should be unlimited")
		}
	}
}

func TestMaxConnsFromEnv(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	for _, tt := range []struct {
		name, value string
		want        int
	}{
		{name: "unset", want: defaultMaxConns},
		{name: "valid", value: "8", want: 8},
		{name: "zero", value: "0", want: defaultMaxConns},
		{name: "invalid", value: "abc", want: defaultMaxConns},
	} {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("EGRESS_MAX_CONNS", tt.value)
			if got := maxConnsFromEnv(logger); got != tt.want {
				t.Fatalf("maxConnsFromEnv() = %d, want %d", got, tt.want)
			}
		})
	}
}
