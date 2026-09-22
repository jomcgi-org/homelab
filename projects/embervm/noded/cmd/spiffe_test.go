package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

type testX509Source struct {
	mu         sync.RWMutex
	svid       *x509svid.SVID
	bundle     *x509bundle.Bundle
	getCount   atomic.Int32
	closeCount atomic.Int32
	lastSerial atomic.Int64
}

func (s *testX509Source) GetX509SVID() (*x509svid.SVID, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	s.getCount.Add(1)
	if s.svid == nil {
		return nil, errors.New("test source has no SVID")
	}
	s.lastSerial.Store(s.svid.Certificates[0].SerialNumber.Int64())
	return s.svid, nil
}

func (s *testX509Source) GetX509BundleForTrustDomain(spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	return s.bundle, nil
}

func (s *testX509Source) Close() error {
	s.closeCount.Add(1)
	return nil
}

func (s *testX509Source) setSVID(svid *x509svid.SVID) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.svid = svid
}

type transportProbeServer struct {
	nodev1.UnimplementedNodeServiceServer
	calls atomic.Int32
}

func (s *transportProbeServer) Prime(context.Context, *nodev1.PrimeRequest) (*nodev1.PrimeResponse, error) {
	s.calls.Add(1)
	return &nodev1.PrimeResponse{VmId: "same-node-service"}, nil
}

func TestSPIFFEKeepaliveMatchesADRRotationBound(t *testing.T) {
	if defaultSPIFFEKeepalive.maxConnectionAge != time.Hour {
		t.Fatalf("MaxConnectionAge = %s, want 1h", defaultSPIFFEKeepalive.maxConnectionAge)
	}
	if defaultSPIFFEKeepalive.maxConnectionAgeGrace != 5*time.Minute {
		t.Fatalf("MaxConnectionAgeGrace = %s, want 5m", defaultSPIFFEKeepalive.maxConnectionAgeGrace)
	}
}

func TestNodedGRPCServersDefaultOffDoesNotOpenWorkloadAPI(t *testing.T) {
	created := false
	servers, err := newNodedGRPCServers(
		context.Background(),
		config.Config{PlaintextGRPCEnabled: true, ListenAddr: "127.0.0.1:0"},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) {
			created = true
			return nil, errors.New("must not be called")
		},
		defaultSPIFFEKeepalive,
	)
	if err != nil {
		t.Fatalf("newNodedGRPCServers: %v", err)
	}
	defer servers.Close()
	if created {
		t.Fatal("disabled SPIFFE listener contacted the Workload API")
	}
	if len(servers.listeners) != 1 || servers.listeners[0].name != "plaintext" {
		t.Fatalf("listeners = %#v, want plaintext only", servers.listeners)
	}
}

func TestNodedGRPCServersRejectEmptyAllowlistBeforeOpeningWorkloadAPI(t *testing.T) {
	created := false
	servers, err := newNodedGRPCServers(
		context.Background(),
		config.Config{
			PlaintextGRPCEnabled: true,
			ListenAddr:           "127.0.0.1:0",
			SPIFFEEnabled:        true,
			TLSListenAddr:        "127.0.0.1:0",
		},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) {
			created = true
			return nil, errors.New("must not be called")
		},
		defaultSPIFFEKeepalive,
	)
	if err == nil || !strings.Contains(err.Error(), "at least one SPIFFE client ID") {
		t.Fatalf("newNodedGRPCServers error = %v, want empty allowlist rejection", err)
	}
	if servers != nil {
		t.Fatal("empty allowlist returned usable servers")
	}
	if created {
		t.Fatal("empty allowlist contacted the Workload API")
	}
}

func TestNodedGRPCServersAuthorizeSPIFFEAndRetainBearerListener(t *testing.T) {
	trustDomain := spiffeid.RequireTrustDomainFromString("test.example")
	caCertificate, caKey := newNodedTestCA(t, 1)
	bundle := x509bundle.FromX509Authorities(trustDomain, []*x509.Certificate{caCertificate})
	serverID := spiffeid.RequireFromString("spiffe://test.example/noded")
	allowedID := spiffeid.RequireFromString("spiffe://test.example/control-plane")
	disallowedID := spiffeid.RequireFromString("spiffe://test.example/other")
	serverSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, serverID, 2), bundle: bundle}
	allowedSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, allowedID, 3), bundle: bundle}
	disallowedSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, disallowedID, 4), bundle: bundle}
	probe := &transportProbeServer{}
	servers, err := newNodedGRPCServers(
		context.Background(),
		config.Config{
			PlaintextGRPCEnabled: true,
			ListenAddr:           "127.0.0.1:0",
			BearerToken:          "node-secret",
			SPIFFEEnabled:        true,
			TLSListenAddr:        "127.0.0.1:0",
			SPIFFEClientIDs:      []spiffeid.ID{allowedID},
		},
		probe,
		func(context.Context) (spiffeX509Source, error) { return serverSource, nil },
		defaultSPIFFEKeepalive,
	)
	if err != nil {
		t.Fatalf("newNodedGRPCServers: %v", err)
	}
	defer servers.Close()
	servers.Serve(slog.New(slog.NewTextHandler(io.Discard, nil)))

	mtlsAddr := grpcListenerAddress(t, servers, "SPIFFE mTLS")
	plaintextAddr := grpcListenerAddress(t, servers, "plaintext")
	allowedConn := dialNodedMTLS(t, mtlsAddr, allowedSource, serverID, true)
	defer allowedConn.Close()
	callPrime(t, allowedConn, false)

	plaintextConn := dialNodedPlaintext(t, plaintextAddr)
	defer plaintextConn.Close()
	callPrime(t, plaintextConn, true)
	if probe.calls.Load() != 2 {
		t.Fatalf("shared NodeService calls = %d, want 2", probe.calls.Load())
	}

	assertMTLSDialFails(t, mtlsAddr, disallowedSource, serverID)

	noCertificate := &tls.Config{
		MinVersion: tls.VersionTLS12,
		RootCAs:    certificatePool(caCertificate),
		ServerName: "localhost",
	}
	assertTLSConfigDialFails(t, mtlsAddr, noCertificate)

	untrustedCA, untrustedKey := newNodedTestCA(t, 10)
	untrustedSource := &testX509Source{
		svid:   newNodedTestSVID(t, untrustedCA, untrustedKey, allowedID, 11),
		bundle: bundle,
	}
	assertMTLSDialFails(t, mtlsAddr, untrustedSource, serverID)

	badCtx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	_, err = nodev1.NewNodeServiceClient(plaintextConn).Prime(badCtx, &nodev1.PrimeRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("plaintext call without bearer status = %v, want Unauthenticated", status.Code(err))
	}
}

func TestNodedGRPCServersConnectionAgeReloadsServerAndClientSVIDs(t *testing.T) {
	trustDomain := spiffeid.RequireTrustDomainFromString("rotation.test")
	caCertificate, caKey := newNodedTestCA(t, 20)
	bundle := x509bundle.FromX509Authorities(trustDomain, []*x509.Certificate{caCertificate})
	serverID := spiffeid.RequireFromString("spiffe://rotation.test/noded")
	clientID := spiffeid.RequireFromString("spiffe://rotation.test/control-plane")
	serverSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, serverID, 21), bundle: bundle}
	clientSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, clientID, 22), bundle: bundle}
	servers, err := newNodedGRPCServers(
		context.Background(),
		config.Config{
			SPIFFEEnabled:   true,
			TLSListenAddr:   "127.0.0.1:0",
			SPIFFEClientIDs: []spiffeid.ID{clientID},
		},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) { return serverSource, nil },
		spiffeKeepaliveConfig{maxConnectionAge: 100 * time.Millisecond, maxConnectionAgeGrace: 50 * time.Millisecond},
	)
	if err != nil {
		t.Fatalf("newNodedGRPCServers: %v", err)
	}
	defer servers.Close()
	servers.Serve(slog.New(slog.NewTextHandler(io.Discard, nil)))
	conn := dialNodedMTLS(t, grpcListenerAddress(t, servers, "SPIFFE mTLS"), clientSource, serverID, true)
	defer conn.Close()
	callPrime(t, conn, false)

	const (
		rotatedClientSerial = int64(23)
		rotatedServerSerial = int64(24)
	)
	clientSource.setSVID(newNodedTestSVID(t, caCertificate, caKey, clientID, rotatedClientSerial))
	serverSource.setSVID(newNodedTestSVID(t, caCertificate, caKey, serverID, rotatedServerSerial))
	deadline := time.Now().Add(5 * time.Second)
	client := nodev1.NewNodeServiceClient(conn)
	for time.Now().Before(deadline) {
		callCtx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		_, callErr := client.Prime(callCtx, &nodev1.PrimeRequest{})
		cancel()
		if callErr != nil && status.Code(callErr) != codes.Unavailable {
			t.Fatalf("Prime while rotating: %v", callErr)
		}
		if clientSource.getCount.Load() >= 2 &&
			clientSource.lastSerial.Load() == rotatedClientSerial &&
			serverSource.getCount.Load() >= 2 &&
			serverSource.lastSerial.Load() == rotatedServerSerial {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf(
		"client SVID source reads = %d, last serial = %d; server reads = %d, last serial = %d; MaxConnectionAge did not force a handshake",
		clientSource.getCount.Load(),
		clientSource.lastSerial.Load(),
		serverSource.getCount.Load(),
		serverSource.lastSerial.Load(),
	)
}

func TestNodedGRPCServersFailClosedAndReleaseResources(t *testing.T) {
	plaintextAddr := reserveTCPAddress(t)
	_, err := newNodedGRPCServers(
		context.Background(),
		config.Config{
			PlaintextGRPCEnabled: true,
			ListenAddr:           plaintextAddr,
			SPIFFEEnabled:        true,
			TLSListenAddr:        "127.0.0.1:0",
		},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) { return nil, errors.New("Workload API unavailable") },
		defaultSPIFFEKeepalive,
	)
	if err == nil {
		t.Fatal("SPIFFE source failure started the plaintext listener")
	}
	assertAddressAvailable(t, plaintextAddr)

	trustDomain := spiffeid.RequireTrustDomainFromString("cleanup.test")
	caCertificate, caKey := newNodedTestCA(t, 30)
	bundle := x509bundle.FromX509Authorities(trustDomain, []*x509.Certificate{caCertificate})
	serverID := spiffeid.RequireFromString("spiffe://cleanup.test/noded")
	clientID := spiffeid.RequireFromString("spiffe://cleanup.test/control")
	blockedTLS, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer blockedTLS.Close()
	tlsBindSource := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, serverID, 31), bundle: bundle}
	plaintextAfterTLSFailure := reserveTCPAddress(t)
	_, err = newNodedGRPCServers(
		context.Background(),
		config.Config{
			PlaintextGRPCEnabled: true,
			ListenAddr:           plaintextAfterTLSFailure,
			SPIFFEEnabled:        true,
			TLSListenAddr:        blockedTLS.Addr().String(),
			SPIFFEClientIDs:      []spiffeid.ID{clientID},
		},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) { return tlsBindSource, nil },
		defaultSPIFFEKeepalive,
	)
	if err == nil {
		t.Fatal("TLS bind conflict silently fell back to plaintext")
	}
	if tlsBindSource.closeCount.Load() != 1 {
		t.Fatalf("X509 source close count after TLS bind failure = %d, want 1", tlsBindSource.closeCount.Load())
	}
	assertAddressAvailable(t, plaintextAfterTLSFailure)

	source := &testX509Source{svid: newNodedTestSVID(t, caCertificate, caKey, serverID, 31), bundle: bundle}
	tlsAddr := reserveTCPAddress(t)
	blockedPlaintext, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer blockedPlaintext.Close()
	_, err = newNodedGRPCServers(
		context.Background(),
		config.Config{
			PlaintextGRPCEnabled: true,
			ListenAddr:           blockedPlaintext.Addr().String(),
			SPIFFEEnabled:        true,
			TLSListenAddr:        tlsAddr,
			SPIFFEClientIDs:      []spiffeid.ID{clientID},
		},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) { return source, nil },
		defaultSPIFFEKeepalive,
	)
	if err == nil {
		t.Fatal("plaintext bind conflict unexpectedly succeeded")
	}
	if source.closeCount.Load() != 1 {
		t.Fatalf("X509 source close count = %d, want 1", source.closeCount.Load())
	}
	assertAddressAvailable(t, tlsAddr)
}

func TestNodedGRPCServersPropagateListenerFailure(t *testing.T) {
	servers, err := newNodedGRPCServers(
		context.Background(),
		config.Config{PlaintextGRPCEnabled: true, ListenAddr: "127.0.0.1:0"},
		&transportProbeServer{},
		func(context.Context) (spiffeX509Source, error) {
			return nil, errors.New("must not be called")
		},
		defaultSPIFFEKeepalive,
	)
	if err != nil {
		t.Fatalf("newNodedGRPCServers: %v", err)
	}
	defer servers.Close()
	errCh := servers.Serve(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := servers.listeners[0].listener.Close(); err != nil {
		t.Fatalf("close listener: %v", err)
	}
	select {
	case serveErr := <-errCh:
		if serveErr == nil || !strings.Contains(serveErr.Error(), "plaintext gRPC listener stopped") {
			t.Fatalf("Serve error = %v, want named plaintext listener failure", serveErr)
		}
	case <-time.After(time.Second):
		t.Fatal("listener failure was not propagated")
	}
}

func grpcListenerAddress(t *testing.T, servers *nodedGRPCServers, name string) string {
	t.Helper()
	for index := range servers.listeners {
		if servers.listeners[index].name == name {
			return servers.listeners[index].listener.Addr().String()
		}
	}
	t.Fatalf("listener %q not found", name)
	return ""
}

func dialNodedMTLS(t *testing.T, address string, source spiffeX509Source, serverID spiffeid.ID, block bool) *grpc.ClientConn {
	t.Helper()
	tlsConfig := tlsconfig.MTLSClientConfig(source, source, tlsconfig.AuthorizeOneOf(serverID))
	options := []grpc.DialOption{grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig))}
	if block {
		options = append(options, grpc.WithBlock())
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(ctx, address, options...)
	if err != nil {
		t.Fatalf("dial mTLS noded: %v", err)
	}
	return conn
}

func dialNodedPlaintext(t *testing.T, address string) *grpc.ClientConn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(
		ctx,
		address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		t.Fatalf("dial plaintext noded: %v", err)
	}
	return conn
}

func callPrime(t *testing.T, conn *grpc.ClientConn, bearer bool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if bearer {
		ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer node-secret"))
	}
	response, err := nodev1.NewNodeServiceClient(conn).Prime(ctx, &nodev1.PrimeRequest{})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	if response.GetVmId() != "same-node-service" {
		t.Fatalf("Prime vm_id = %q, want same-node-service", response.GetVmId())
	}
}

func assertMTLSDialFails(t *testing.T, address string, source spiffeX509Source, serverID spiffeid.ID) {
	t.Helper()
	tlsConfig := tlsconfig.MTLSClientConfig(source, source, tlsconfig.AuthorizeOneOf(serverID))
	assertTLSConfigDialFails(t, address, tlsConfig)
}

func assertTLSConfigDialFails(t *testing.T, address string, tlsConfig *tls.Config) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	conn, err := grpc.DialContext(
		ctx,
		address,
		grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)),
		grpc.WithBlock(),
	)
	if err == nil {
		_ = conn.Close()
		t.Fatal("unauthorized TLS client connected")
	}
}

func reserveTCPAddress(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	return address
}

func assertAddressAvailable(t *testing.T, address string) {
	t.Helper()
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("address %s was leaked: %v", address, err)
	}
	_ = listener.Close()
}

func certificatePool(certificates ...*x509.Certificate) *x509.CertPool {
	pool := x509.NewCertPool()
	for _, certificate := range certificates {
		pool.AddCert(certificate)
	}
	return pool
}

func newNodedTestCA(t *testing.T, serial int64) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(serial),
		Subject:               pkix.Name{CommonName: "noded test CA"},
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return certificate, key
}

func newNodedTestSVID(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, id spiffeid.ID, serial int64) *x509svid.SVID {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(serial),
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		URIs:                  []*url.URL{id.URL()},
		DNSNames:              []string{"localhost"},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &x509svid.SVID{ID: id, Certificates: []*x509.Certificate{certificate}, PrivateKey: key}
}
