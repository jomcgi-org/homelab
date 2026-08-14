package main

import (
	"bufio"
	"bytes"
	"io"
	"reflect"
	"strings"
	"testing"
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
