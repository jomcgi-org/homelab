package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

type staticX509Source struct {
	svid   *x509svid.SVID
	bundle *x509bundle.Bundle
}

func (s *staticX509Source) GetX509SVID() (*x509svid.SVID, error) {
	return s.svid, nil
}

func (s *staticX509Source) GetX509BundleForTrustDomain(spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	return s.bundle, nil
}

func TestConfiguredListenersRequiresClientIDsForTLS(t *testing.T) {
	t.Setenv("BROKER_TLS_LISTEN_ADDR", ":8443")
	t.Setenv("BROKER_SPIFFE_CLIENT_IDS", "")

	_, err := configuredListeners()
	if err == nil || !strings.Contains(err.Error(), "BROKER_SPIFFE_CLIENT_IDS is required when BROKER_TLS_LISTEN_ADDR is set") {
		t.Fatalf("configuredListeners() error = %v, want missing client IDs error", err)
	}
}

func TestPlaintextHandlerRequiresMTLSForTokenWhenEnabled(t *testing.T) {
	s := newServerWithStoredGrant(t)
	handler := s.grantsHandler(false, true)

	token := performRequest(handler, http.MethodGet, "/grants/codex-cluster/token")
	if token.Code != http.StatusForbidden || token.Body.String() != "token endpoint requires mTLS on the SPIFFE port" {
		t.Fatalf("token response = %d %q", token.Code, token.Body.String())
	}
	refresh := performRequest(handler, http.MethodPost, "/grants/codex-cluster/refresh")
	if refresh.Code != http.StatusOK {
		t.Fatalf("refresh response = %d %s", refresh.Code, refresh.Body.String())
	}
	status := performRequest(handler, http.MethodGet, "/grants/codex-cluster/login/status")
	if status.Code != http.StatusOK {
		t.Fatalf("login status response = %d %s", status.Code, status.Body.String())
	}
}

func TestPlaintextHandlerServesTokenWhenMTLSDisabled(t *testing.T) {
	response := performRequest(
		newServerWithStoredGrant(t).grantsHandler(false, false),
		http.MethodGet,
		"/grants/codex-cluster/token",
	)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"access_token":"stored-access-token"`) {
		t.Fatalf("token response = %d %s", response.Code, response.Body.String())
	}
}

func TestMTLSHandlerAuthorizesConfiguredClient(t *testing.T) {
	trustDomain := spiffeid.RequireTrustDomainFromString("test.example")
	caCertificate, caKey := newTestCA(t)
	bundle := x509bundle.FromX509Authorities(trustDomain, []*x509.Certificate{caCertificate})
	serverID := spiffeid.RequireFromString("spiffe://test.example/server")
	allowedID := spiffeid.RequireFromString("spiffe://test.example/allowed")
	disallowedID := spiffeid.RequireFromString("spiffe://test.example/disallowed")
	serverSource := &staticX509Source{svid: newTestSVID(t, caCertificate, caKey, serverID, 2), bundle: bundle}
	allowedSource := &staticX509Source{svid: newTestSVID(t, caCertificate, caKey, allowedID, 3), bundle: bundle}
	disallowedSource := &staticX509Source{svid: newTestSVID(t, caCertificate, caKey, disallowedID, 4), bundle: bundle}

	testServer := httptest.NewUnstartedServer(newServerWithStoredGrant(t).grantsHandler(true, true))
	testServer.TLS = tlsconfig.MTLSServerConfig(serverSource, serverSource, tlsconfig.AuthorizeOneOf(allowedID))
	testServer.TLS.Certificates = []tls.Certificate{{
		Certificate: [][]byte{serverSource.svid.Certificates[0].Raw},
		PrivateKey:  serverSource.svid.PrivateKey,
		Leaf:        serverSource.svid.Certificates[0],
	}}
	testServer.StartTLS()
	defer testServer.Close()

	allowedClient := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsconfig.MTLSClientConfig(
		allowedSource,
		allowedSource,
		tlsconfig.AuthorizeOneOf(serverID),
	)}}
	allowedResponse, err := allowedClient.Get(testServer.URL + "/grants/codex-cluster/token")
	if err != nil {
		t.Fatalf("allowed client request failed: %v", err)
	}
	defer allowedResponse.Body.Close()
	if allowedResponse.StatusCode != http.StatusOK {
		t.Fatalf("allowed client status = %d, want %d", allowedResponse.StatusCode, http.StatusOK)
	}

	disallowedClient := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsconfig.MTLSClientConfig(
		disallowedSource,
		disallowedSource,
		tlsconfig.AuthorizeOneOf(serverID),
	)}}
	_, err = disallowedClient.Get(testServer.URL + "/grants/codex-cluster/token")
	if err == nil {
		t.Fatal("disallowed client request succeeded, want TLS handshake error")
	}
}

func TestTokenRequestCounterIncrementsForBothOutcomes(t *testing.T) {
	s := newServerWithStoredGrant(t)
	s.tokenRequests = newTokenRequestsCounter()
	registry := prometheus.NewRegistry()
	registry.MustRegister(s.tokenRequests)

	plaintext := performRequest(s.grantsHandler(false, true), http.MethodGet, "/grants/codex-cluster/token")
	if plaintext.Code != http.StatusForbidden {
		t.Fatalf("plaintext status = %d, want %d", plaintext.Code, http.StatusForbidden)
	}
	mtls := performRequest(s.grantsHandler(true, true), http.MethodGet, "/grants/codex-cluster/token")
	if mtls.Code != http.StatusOK {
		t.Fatalf("mTLS status = %d, want %d", mtls.Code, http.StatusOK)
	}

	expected := `# HELP tokenbroker_token_requests_total Token endpoint requests by listener and authorization outcome.
# TYPE tokenbroker_token_requests_total counter
tokenbroker_token_requests_total{listener="mtls",outcome="served"} 1
tokenbroker_token_requests_total{listener="plaintext",outcome="rejected_plaintext"} 1
`
	if err := testutil.GatherAndCompare(registry, strings.NewReader(expected), "tokenbroker_token_requests_total"); err != nil {
		t.Fatal(err)
	}
}

func TestWaitForX509SourceFailsClosedOnTimeout(t *testing.T) {
	timeout := 10 * time.Millisecond
	started := time.Now()
	source, err := waitForX509Source(timeout, func(ctx context.Context) (*workloadapi.X509Source, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	})
	if err == nil {
		if source != nil {
			source.Close()
		}
		t.Fatal("waitForX509Source() succeeded for a source that never yielded an SVID")
	}
	if elapsed := time.Since(started); elapsed < timeout || elapsed > time.Second {
		t.Fatalf("waitForX509Source() returned after %s, want between %s and 1s", elapsed, timeout)
	}
}

func newServerWithStoredGrant(t *testing.T) *server {
	t.Helper()
	grant := store.Grant{
		Name:         "codex-cluster",
		ProviderName: "test",
		LastRefresh:  time.Now().UTC(),
		TokenBundle: store.TokenBundle{
			AccessToken:  "stored-access-token",
			RefreshToken: "stored-refresh-token",
			ExpiresAt:    time.Now().Add(time.Hour),
		},
	}
	return newTestServer(
		&fakeStore{grants: map[string]store.Grant{"codex-cluster": grant}},
		&fakeAdapter{deviceCode: provider.DeviceCodeResponse{}},
	)
}

func performRequest(handler http.Handler, method, path string) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(method, path, nil))
	return recorder
}

func newTestCA(t *testing.T) (*x509.Certificate, *ecdsa.PrivateKey) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test CA"},
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

func newTestSVID(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, id spiffeid.ID, serial int64) *x509svid.SVID {
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
