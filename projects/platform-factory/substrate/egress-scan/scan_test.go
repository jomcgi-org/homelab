package main

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
)

func TestCheck(t *testing.T) {
	// A synthetic example of a recognized credential format.
	fixture := "gh" + "p_" + "8Jdm9u1Qa7bWdF2p7XZ9sWy7Qnf2gXMbRFqa"
	escaped := `\u0067` + fixture[1:]
	cases := []struct {
		name    string
		target  string
		media   string
		body    string
		headers map[string]string
		partial bool
		status  typev3.StatusCode
	}{
		{name: "clean MCP call", target: "/mcp/homelab/", media: "application/json", body: `{"jsonrpc":"2.0","method":"tools/call","params":{"name":"retain","arguments":{"content":"Test passed"}}}`},
		{name: "escape decoded", target: "/", media: "application/json", body: `{"content":"` + escaped + `"}`, status: typev3.StatusCode_Forbidden},
		{name: "suppression ignored", target: "/", media: "text/plain", body: fixture + " # gitleaks:allow", status: typev3.StatusCode_Forbidden},
		{name: "duplicate keys scanned", target: "/", media: "application/json", body: `{"content":"` + escaped + `","content":"clean"}`, status: typev3.StatusCode_Forbidden},
		{name: "query decoded", target: "/?label=%67" + fixture[1:], status: typev3.StatusCode_Forbidden},
		{name: "path decoded", target: "/%67" + fixture[1:], status: typev3.StatusCode_Forbidden},
		{name: "joined header", target: "/", headers: map[string]string{"x-extra-label": "one," + fixture}, status: typev3.StatusCode_Forbidden},
		{name: "authorization placeholder", target: "/", headers: map[string]string{"authorization": "Bearer -"}},
		{name: "compressed", target: "/", media: "text/plain", body: "clean", headers: map[string]string{"content-encoding": "gzip"}, status: typev3.StatusCode_UnsupportedMediaType},
		{name: "binary format", target: "/", media: "application/octet-stream", body: "content", status: typev3.StatusCode_UnsupportedMediaType},
		{name: "malformed JSON", target: "/", media: "application/json", body: `{"content":`, status: typev3.StatusCode_BadRequest},
		{name: "multiple JSON documents", target: "/", media: "application/json", body: `{"content":"clean"} {"name":"retain"}`, status: typev3.StatusCode_BadRequest},
		{name: "partial", target: "/", media: "text/plain", body: "partial", partial: true, status: typev3.StatusCode_BadRequest},
		{name: "size limit", target: "/", media: "text/plain", body: strings.Repeat("x", maxBody+1), status: typev3.StatusCode_PayloadTooLarge},
	}

	server := newTestScanner(t)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := &authv3.AttributeContext_HttpRequest{
				Method: "POST", Path: tc.target, RawBody: []byte(tc.body),
				Size: int64(len(tc.body)), Headers: map[string]string{},
			}
			for name, value := range tc.headers {
				req.Headers[name] = value
			}
			if tc.media != "" {
				req.Headers["content-type"] = tc.media
			}
			if tc.partial {
				req.Size = -1
			}
			result, err := server.Check(context.Background(), request(req))
			if err != nil {
				t.Fatal(err)
			}
			if tc.status == 0 {
				if result.GetStatus().GetCode() != int32(codes.OK) || result.GetDeniedResponse() != nil {
					t.Fatalf("clean request blocked with code %d", result.GetStatus().GetCode())
				}
				return
			}
			if result.GetStatus().GetCode() == int32(codes.OK) || result.GetDeniedResponse().GetStatus().GetCode() != tc.status {
				t.Fatalf("denied HTTP status %d, want %d", result.GetDeniedResponse().GetStatus().GetCode(), tc.status)
			}
			if strings.Contains(result.String(), fixture) {
				t.Error("denial includes credential")
			}
		})
	}
}

func TestCheckOverGRPC(t *testing.T) {
	backend := newTestScanner(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxCheckMessage))
	t.Cleanup(server.Stop)
	authv3.RegisterAuthorizationServer(server, backend)
	go server.Serve(listener)

	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	client := authv3.NewAuthorizationClient(conn)

	check := func() (*authv3.CheckResponse, error) {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		return client.Check(ctx, request(&authv3.AttributeContext_HttpRequest{Method: "GET", Path: "/"}))
	}
	result, err := check()
	if err != nil || result.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("wire check did not pass: %v", err)
	}

	for range cap(backend.slots) {
		backend.slots <- struct{}{}
	}
	result, err = check()
	if err != nil || result.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_ServiceUnavailable {
		t.Fatalf("saturated check did not deny: %v", err)
	}
}

func TestOperatorRules(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gitleaks.toml")
	const config = `title = "Operator test rules"

[[rules]]
id = "test-credential"
description = "Synthetic pattern added by operator"
regex = 'TEST_CREDENTIAL_[A-Z0-9]{12}'
`
	if err := os.WriteFile(path, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	detector, err := newDetector(path)
	if err != nil {
		t.Fatal(err)
	}
	if matched, err := detector.find(context.Background(), "TEST_CREDENTIAL_AB12CD34EF56"); !matched || err != nil {
		t.Fatalf("configured rule did not match: %v", err)
	}
	if err := os.WriteFile(path, []byte(config+`path = '\.go$'`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newDetector(path); err == nil {
		t.Fatal("configured a rule that requires a repository path")
	}
	if err := os.WriteFile(path, []byte("rules = []"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := newDetector(path); err == nil {
		t.Fatal("configured an empty ruleset")
	}
}

func TestDenseMatchesReleaseScanSlot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "gitleaks.toml")
	if err := os.WriteFile(path, []byte(`[[rules]]
id = 'synthetic-marker'
regex = '(?m)^MARK:([a-z]+)'
[[rules.allowlists]]
regexTarget = 'line'
regexes = ['approved-line']
`), 0o600); err != nil {
		t.Fatal(err)
	}
	detector, err := newDetector(path)
	if err != nil {
		t.Fatal(err)
	}
	backend := &scanner{detector: detector, slots: make(chan struct{}, 1)}
	check := func(body string, code codes.Code) {
		t.Helper()
		reply, err := backend.Check(context.Background(), request(&authv3.AttributeContext_HttpRequest{
			Method: "POST", Path: "/", Headers: map[string]string{"content-type": "text/plain"},
			Size: int64(len(body)), RawBody: []byte(body),
		}))
		if err != nil || codes.Code(reply.GetStatus().GetCode()) != code {
			t.Fatalf("expected %v, got %v: %v", code, reply.GetStatus(), err)
		}
	}
	check("MARK:example approved-line MARK:other", codes.OK)
	check("MARK:example approved-line\nMARK:other", codes.PermissionDenied)
	check(strings.Repeat("MARK:other\n", 25000), codes.PermissionDenied)
	check(strings.Repeat("MARK:example approved-line\n", 17000), codes.ResourceExhausted)
	check("clean text", codes.OK)
}

func TestRegexReaderStopsAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	reader := newScanReader(ctx, strings.Repeat("abcd", 1024)+"MARK:other")
	if _, _, err := reader.ReadRune(); err != nil {
		cancel()
		t.Fatal(err)
	}
	cancel()
	match := regexp.MustCompile(`MARK:[a-z]+`).FindReaderIndex(reader)
	if match != nil || reader.count > 256 {
		t.Fatal("regex kept reading input after cancellation")
	}
}

func request(http *authv3.AttributeContext_HttpRequest) *authv3.CheckRequest {
	return &authv3.CheckRequest{Attributes: &authv3.AttributeContext{
		Request: &authv3.AttributeContext_Request{Http: http},
	}}
}

func newTestScanner(t *testing.T) *scanner {
	t.Helper()
	detector, err := newDetector("")
	if err != nil {
		t.Fatal(err)
	}
	return &scanner{detector: detector, slots: make(chan struct{}, 8)}
}
