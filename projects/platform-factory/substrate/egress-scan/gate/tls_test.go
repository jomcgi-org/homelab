package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate/ateapi"
)

type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newTestCA(t testing.TB, name string) *testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: name},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return &testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

// issue returns a certificate and key in one PEM bundle, as kubelet writes a
// podCertificate projection, and the parsed pair for a server.
func (ca *testCA) issue(t testing.TB, dns string, uri string, usage x509.ExtKeyUsage) ([]byte, tls.Certificate) {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: dns},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage},
	}
	if dns != "" {
		tpl.DNSNames = []string{dns}
	}
	if uri != "" {
		u, _ := parseURL(uri)
		tpl.URIs = append(tpl.URIs, u)
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, _ := x509.MarshalPKCS8PrivateKey(key)
	bundle := append(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}),
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})...)
	pair, err := tls.X509KeyPair(bundle, bundle)
	if err != nil {
		t.Fatal(err)
	}
	return bundle, pair
}

type routerFixture struct {
	srv      *httptest.Server
	dir      string
	caFile   string
	certFile string
	src      *tlsSource

	mu      sync.Mutex
	headers []http.Header
	bodies  []string
	peers   []*x509.Certificate
	handler http.HandlerFunc
}

// newRouterFixture starts a TLS server that requires a client certificate, as
// the router does (the router checks the SPIFFE ID).
func newRouterFixture(t *testing.T, serverCA, clientCA *testCA, serverName string) *routerFixture {
	t.Helper()
	f := &routerFixture{dir: t.TempDir()}
	_, serverPair := serverCA.issue(t, serverName, "", x509.ExtKeyUsageServerAuth)
	clients := x509.NewCertPool()
	clients.AddCert(clientCA.cert)
	f.srv = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		f.mu.Lock()
		f.headers = append(f.headers, r.Header.Clone())
		f.bodies = append(f.bodies, string(data))
		f.peers = append(f.peers, r.TLS.PeerCertificates[0])
		h := f.handler
		f.mu.Unlock()
		if h != nil {
			h(w, r)
			return
		}
		_, _ = io.WriteString(w, ".\n{\"results\":[]}\n")
	}))
	f.srv.TLS = &tls.Config{Certificates: []tls.Certificate{serverPair}, ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: clients}
	f.srv.StartTLS()
	t.Cleanup(f.srv.Close)
	f.caFile = filepath.Join(f.dir, "trust-bundle.pem")
	f.certFile = filepath.Join(f.dir, "credential-bundle.pem")
	if err := os.WriteFile(f.caFile, serverCA.pem, 0o600); err != nil {
		t.Fatal(err)
	}
	bundle, _ := clientCA.issue(t, "", "spiffe://cluster.local/ns/ate-system/sa/engine-gate", x509.ExtKeyUsageClientAuth)
	if err := os.WriteFile(f.certFile, bundle, 0o600); err != nil {
		t.Fatal(err)
	}
	f.src = newTLSSource(f.caFile, f.certFile)
	return f
}

func (f *routerFixture) router(t *testing.T, serverName string, maxResponse int64) *httpRouter {
	t.Helper()
	r, err := newHTTPRouter(f.srv.URL, f.src.config(serverName, true), maxResponse)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(r.client.CloseIdleConnections)
	return r
}

func TestRouterSendsMTLSRequestWithOnlyFixedHeaders(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	f := newRouterFixture(t, ca, ca, "atenet-router.ate-system.svc")
	r := f.router(t, "atenet-router.ate-system.svc", 1<<20)
	res, err := r.Send(context.Background(), "egress-scan-engine/eg-gate-0-1-1", []byte("hello"))
	if err != nil {
		t.Fatal(err)
	}
	if string(res.Body) != ".\n{\"results\":[]}\n" || res.Connect <= 0 || res.Engine <= 0 {
		t.Fatalf("result %+v", res)
	}
	if got := f.headers[0].Get(targetActorHeader); got != "egress-scan-engine/eg-gate-0-1-1" {
		t.Fatalf("ate-target-actor = %q", got)
	}
	if f.bodies[0] != "hello" {
		t.Fatalf("body %q", f.bodies[0])
	}
	if len(f.peers[0].URIs) != 1 || f.peers[0].URIs[0].String() != "spiffe://cluster.local/ns/ate-system/sa/engine-gate" {
		t.Fatal("the router saw no client SPIFFE ID")
	}
	for k := range f.headers[0] {
		switch http.CanonicalHeaderKey(k) {
		case "Ate-Target-Actor", "Content-Type", "User-Agent", "Content-Length", "Accept-Encoding":
		default:
			t.Fatalf("unexpected header %s", k)
		}
	}
	// A second call reuses the connection.
	res, err = r.Send(context.Background(), "egress-scan-engine/eg-gate-0-1-2", []byte("again"))
	if err != nil || res.Connect != 0 {
		t.Fatalf("second call: %+v %v", res, err)
	}
}

func TestRouterRefusesAnUntrustedOrMisnamedServer(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	other := newTestCA(t, "other")
	// The server certificate comes from another CA.
	f := newRouterFixture(t, other, ca, "atenet-router.ate-system.svc")
	if err := os.WriteFile(f.caFile, ca.pem, 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := f.router(t, "atenet-router.ate-system.svc", 1<<20).Send(context.Background(), "a/b", []byte("body"))
	if se, ok := err.(*sendError); !ok || se.Kind != sendUnsent {
		t.Fatalf("untrusted server: %v", err)
	}
	// The name does not match.
	g := newRouterFixture(t, ca, ca, "someone-else.svc")
	_, err = g.router(t, "atenet-router.ate-system.svc", 1<<20).Send(context.Background(), "a/b", []byte("body"))
	if se, ok := err.(*sendError); !ok || se.Kind != sendUnsent {
		t.Fatalf("wrong name: %v", err)
	}
	if len(f.bodies)+len(g.bodies) != 0 {
		t.Fatal("a body reached a server that failed verification")
	}
}

func TestRouterFollowsCertificateAndTrustRotation(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	f := newRouterFixture(t, ca, ca, "atenet-router.ate-system.svc")
	r := f.router(t, "atenet-router.ate-system.svc", 1<<20)
	if _, err := r.Send(context.Background(), "a/b", []byte("1")); err != nil {
		t.Fatal(err)
	}
	r.client.CloseIdleConnections()
	// kubelet rotates the pod certificate in place.
	first := f.peers[0].SerialNumber
	time.Sleep(10 * time.Millisecond)
	bundle, _ := ca.issue(t, "", "spiffe://cluster.local/ns/ate-system/sa/engine-gate", x509.ExtKeyUsageClientAuth)
	if err := os.WriteFile(f.certFile, bundle, 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(f.certFile, future, future)
	if _, err := r.Send(context.Background(), "a/b", []byte("2")); err != nil {
		t.Fatal(err)
	}
	if f.peers[1].SerialNumber.Cmp(first) == 0 {
		t.Fatal("the gate kept its old client certificate")
	}
	// The trust bundle rotates to a CA that no longer signs the server.
	r.client.CloseIdleConnections()
	other := newTestCA(t, "next")
	if err := os.WriteFile(f.caFile, other.pem, 0o600); err != nil {
		t.Fatal(err)
	}
	_ = os.Chtimes(f.caFile, future.Add(time.Second), future.Add(time.Second))
	if _, err := r.Send(context.Background(), "a/b", []byte("3")); err == nil {
		t.Fatal("the gate kept its old trust bundle")
	}
}

func TestRouterClassifiesFailures(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	f := newRouterFixture(t, ca, ca, "atenet-router.ate-system.svc")
	r := f.router(t, "atenet-router.ate-system.svc", 64)
	for _, tc := range []struct {
		name    string
		handler http.HandlerFunc
		kind    string
		status  int
		retry   bool
	}{
		{"status 502", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(502) }, sendStatus, 502, true},
		{"status 404", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(404) }, sendStatus, 404, true},
		{"status 400", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(400) }, sendStatus, 400, false},
		{"status 500", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(500) }, sendStatus, 500, false},
		{"redirect", func(w http.ResponseWriter, _ *http.Request) {
			http.Redirect(w, &http.Request{}, "https://elsewhere.invalid/", http.StatusFound)
		}, sendStatus, 302, false},
		{"closed before any reply", func(w http.ResponseWriter, _ *http.Request) {
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
		}, sendNoReply, 0, true},
		{"cut off", func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Length", "50")
			w.WriteHeader(200)
			_, _ = w.Write([]byte("{\"res"))
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
		}, sendRead, 0, true},
		{"too large", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(strings.Repeat("a", 65))) }, sendTooLarge, 0, false},
	} {
		f.mu.Lock()
		f.handler = tc.handler
		f.mu.Unlock()
		_, err := r.Send(context.Background(), "a/b", []byte("body"))
		se, ok := err.(*sendError)
		if !ok || se.Kind != tc.kind || se.Status != tc.status || se.retryable() != tc.retry {
			t.Fatalf("%s: got %#v", tc.name, err)
		}
		if strings.Contains(err.Error(), "127.0.0.1") {
			t.Fatalf("%s: the error names an address: %v", tc.name, err)
		}
	}
	// At the limit is fine.
	f.mu.Lock()
	f.handler = func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(strings.Repeat("a", 64))) }
	f.mu.Unlock()
	if res, err := r.Send(context.Background(), "a/b", []byte("body")); err != nil || len(res.Body) != 64 {
		t.Fatalf("at the limit: %v", err)
	}
}

func TestRouterDeadlineAndUnreachable(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	f := newRouterFixture(t, ca, ca, "atenet-router.ate-system.svc")
	r := f.router(t, "atenet-router.ate-system.svc", 1<<20)
	f.mu.Lock()
	f.handler = func(w http.ResponseWriter, req *http.Request) { <-req.Context().Done() }
	f.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_, err := r.Send(ctx, "a/b", []byte("x"))
	if se, ok := err.(*sendError); !ok || se.Kind != sendTimeout {
		t.Fatalf("deadline: %v", err)
	}
	// Nothing listens: the body never left.
	lis, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := lis.Addr().String()
	lis.Close()
	dead, err := newHTTPRouter("https://"+addr, f.src.config("atenet-router.ate-system.svc", true), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	_, err = dead.Send(context.Background(), "a/b", []byte("x"))
	if se, ok := err.(*sendError); !ok || se.Kind != sendUnsent || se.retryable() {
		t.Fatalf("unreachable: %v", err)
	}
}

func TestRouterURLMustBeHTTPS(t *testing.T) {
	for _, u := range []string{"http://router", "https://user:pw@router", "ftp://x", "https://", "::"} {
		if _, err := newHTTPRouter(u, &tls.Config{}, 1); err == nil {
			t.Fatalf("%q accepted", u)
		}
	}
}

// grpcFixture is a TLS ate API that records the bearer token it receives.
type grpcFixture struct {
	ateapi.UnimplementedControlServer
	mu     sync.Mutex
	tokens []string
}

func (g *grpcFixture) GetActor(ctx context.Context, _ *ateapi.GetActorRequest) (*ateapi.Actor, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	g.mu.Lock()
	g.tokens = append(g.tokens, strings.Join(md.Get("authorization"), ","))
	g.mu.Unlock()
	return &ateapi.Actor{Metadata: &ateapi.ResourceMetadata{Name: "x"}}, nil
}

func TestAPIClientVerifiesTheServerBeforeSendingItsToken(t *testing.T) {
	ca := newTestCA(t, "servicedns")
	other := newTestCA(t, "other")
	for _, tc := range []struct {
		name     string
		signer   *testCA
		dns      string
		wantCall bool
	}{
		{"trusted", ca, "api.ate-system.svc", true},
		{"untrusted CA", other, "api.ate-system.svc", false},
		{"wrong name", ca, "evil.svc", false},
	} {
		_, pair := tc.signer.issue(t, tc.dns, "", x509.ExtKeyUsageServerAuth)
		fx := &grpcFixture{}
		srv := grpc.NewServer(grpc.Creds(credentials.NewServerTLSFromCert(&pair)))
		ateapi.RegisterControlServer(srv, fx)
		lis, _ := net.Listen("tcp", "127.0.0.1:0")
		go srv.Serve(lis)
		dir := t.TempDir()
		_ = os.WriteFile(filepath.Join(dir, "ca.pem"), ca.pem, 0o600)
		_ = os.WriteFile(filepath.Join(dir, "token"), []byte("sa-token\n"), 0o600)
		cfg := testConfig(t)
		cfg.APIEndpoint = "passthrough:///" + lis.Addr().String()
		cfg.APITokenFile = filepath.Join(dir, "token")
		conn, err := dialAPI(cfg, newTLSSource(filepath.Join(dir, "ca.pem"), filepath.Join(dir, "none")))
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		_, err = newGRPCAPI(conn, "egress-scan-engine", "engine").Get(ctx, "x")
		cancel()
		conn.Close()
		srv.Stop()
		fx.mu.Lock()
		got := append([]string(nil), fx.tokens...)
		fx.mu.Unlock()
		if tc.wantCall && (err != nil || len(got) != 1 || got[0] != "Bearer sa-token") {
			t.Fatalf("%s: err %v tokens %v", tc.name, err, got)
		}
		if !tc.wantCall && (err == nil || len(got) != 0) {
			t.Fatalf("%s: the token reached an unverified server (err %v, tokens %v)", tc.name, err, got)
		}
	}
}

func TestBearerFileReadsEachCallAndRefusesEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	b := bearerFile{path: path, requireTLS: true}
	if _, err := b.GetRequestMetadata(context.Background()); err == nil {
		t.Fatal("missing token file accepted")
	}
	_ = os.WriteFile(path, []byte(" \n"), 0o600)
	if _, err := b.GetRequestMetadata(context.Background()); err == nil {
		t.Fatal("empty token accepted")
	}
	_ = os.WriteFile(path, []byte("one\n"), 0o600)
	md, _ := b.GetRequestMetadata(context.Background())
	_ = os.WriteFile(path, []byte("two\n"), 0o600)
	md2, _ := b.GetRequestMetadata(context.Background())
	if md["authorization"] != "Bearer one" || md2["authorization"] != "Bearer two" || !b.RequireTransportSecurity() {
		t.Fatalf("%v %v", md, md2)
	}
}

func TestAPIErrorsKeepTheCodeAndDropTheMessage(t *testing.T) {
	if mapErr(nil) != nil {
		t.Fatal("nil")
	}
	if mapErr(status.Error(codes.NotFound, "secret detail")) != errNotFound {
		t.Fatal("not found")
	}
	err := mapErr(status.Error(codes.ResourceExhausted, "secret detail"))
	if !strings.Contains(err.Error(), "ResourceExhausted") || strings.Contains(err.Error(), "secret") {
		t.Fatal(err)
	}
	if got := mapErr(status.Error(codes.PermissionDenied, "secret detail")); strings.Contains(got.Error(), "secret") {
		t.Fatal(got)
	}
}

func parseURL(s string) (*url.URL, error) { return url.Parse(s) }
