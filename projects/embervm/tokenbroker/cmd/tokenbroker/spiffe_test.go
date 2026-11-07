package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"io"
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

	t.Setenv("BROKER_SPIFFE_CLIENT_IDS", "not-a-spiffe-id")
	_, err = configuredListeners()
	if err == nil || !strings.Contains(err.Error(), "invalid BROKER_SPIFFE_CLIENT_IDS entry") {
		t.Fatalf("configuredListeners() error = %v, want invalid client ID error", err)
	}
}

func TestConfiguredListenersRejectsPlaintextRetirementWithoutTLS(t *testing.T) {
	t.Setenv("BROKER_TLS_LISTEN_ADDR", "")
	t.Setenv("BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES", "true")

	_, err := configuredListeners()
	if err == nil || !strings.Contains(err.Error(), "requires BROKER_TLS_LISTEN_ADDR") {
		t.Fatalf("configuredListeners() error = %v, want TLS listener requirement", err)
	}
}

func TestConfiguredListenersRejectsInvalidPlaintextRetirement(t *testing.T) {
	t.Setenv("BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES", "yes")

	_, err := configuredListeners()
	if err == nil || !strings.Contains(err.Error(), "must be true or false") {
		t.Fatalf("configuredListeners() error = %v, want boolean validation", err)
	}
}

func TestConfiguredListenersStrictLoginFailsClosed(t *testing.T) {
	const tokenID = "spiffe://embervm.jomcgi.dev/ns/embervm-dev/sa/embervm-dev-embervm-noded"
	const loginID = "spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith"
	for _, test := range []struct {
		name, required, tlsAddress, loginIDs, wantError string
	}{
		{"no TLS", "true", "", loginID, "requires BROKER_TLS_LISTEN_ADDR"},
		{"no login identities", "true", ":8443", "", "BROKER_LOGIN_SPIFFE_CLIENT_IDS is required"},
		{"invalid identity", "true", ":8443", "monolith", "invalid BROKER_LOGIN_SPIFFE_CLIENT_IDS entry"},
		{"blank identity", "true", ":8443", loginID + ",", "invalid BROKER_LOGIN_SPIFFE_CLIENT_IDS entry"},
		{"invalid boolean", "yes", ":8443", loginID, "BROKER_LOGIN_AUTH_REQUIRED must be true or false"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES", "false")
			t.Setenv("BROKER_TLS_LISTEN_ADDR", test.tlsAddress)
			t.Setenv("BROKER_SPIFFE_CLIENT_IDS", tokenID)
			t.Setenv("BROKER_LOGIN_AUTH_REQUIRED", test.required)
			t.Setenv("BROKER_LOGIN_SPIFFE_CLIENT_IDS", test.loginIDs)
			_, err := configuredListeners()
			if err == nil || !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("configuredListeners() error = %v, want %q", err, test.wantError)
			}
		})
	}

	t.Setenv("BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES", "false")
	t.Setenv("BROKER_TLS_LISTEN_ADDR", ":8443")
	t.Setenv("BROKER_SPIFFE_CLIENT_IDS", tokenID)
	t.Setenv("BROKER_LOGIN_AUTH_REQUIRED", "true")
	t.Setenv("BROKER_LOGIN_SPIFFE_CLIENT_IDS", " "+loginID+" ")
	listeners, err := configuredListeners()
	if err != nil {
		t.Fatal(err)
	}
	if !listeners.loginAuthRequired || len(listeners.spiffeClientIDs) != 1 || len(listeners.loginSpiffeClientIDs) != 1 {
		t.Fatalf("strict listeners did not retain separate client scopes: %+v", listeners)
	}
	if ids := listeners.mtlsClientIDs(); len(ids) != 2 || ids[0].String() != tokenID || ids[1].String() != loginID {
		t.Fatalf("TLS handshake identities = %v, want token and login identities", ids)
	}

	t.Setenv("BROKER_LOGIN_AUTH_REQUIRED", "false")
	listeners, err = configuredListeners()
	if err != nil {
		t.Fatal(err)
	}
	if listeners.loginAuthRequired || len(listeners.loginSpiffeClientIDs) != 0 || len(listeners.mtlsClientIDs()) != 1 {
		t.Fatalf("disabled login auth changed legacy identity scope: %+v", listeners)
	}
}

func TestRetiredPlaintextRejectsProtectedRoutesButKeepsOperations(t *testing.T) {
	s := newServerWithStoredGrant(t)
	handler := s.plaintextMux(listenerConfig{
		tlsListenAddr:            ":8443",
		retirePlaintextProtected: true,
	})

	for _, request := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/grants/codex-cluster/login/start"},
		{http.MethodGet, "/grants/codex-cluster/login/status"},
		{http.MethodPost, "/grants/codex-cluster/refresh"},
		{http.MethodGet, "/quota"},
		{http.MethodPost, "/quota/codex"},
	} {
		response := performRequest(handler, request.method, request.path)
		if response.Code != http.StatusForbidden || !strings.Contains(response.Body.String(), "plaintext_retired") {
			t.Errorf("%s %s = %d %q, want 403 plaintext_retired", request.method, request.path, response.Code, response.Body.String())
		}
	}
	if health := performRequest(handler, http.MethodGet, "/healthz"); health.Code != http.StatusOK {
		t.Errorf("health status = %d, want %d", health.Code, http.StatusOK)
	}
	if metrics := performRequest(handler, http.MethodGet, "/metrics"); metrics.Code != http.StatusOK {
		t.Errorf("metrics status = %d, want %d", metrics.Code, http.StatusOK)
	}
}

func TestPlaintextHandlerCompatibilityWhenMTLSEnabled(t *testing.T) {
	pollWait := make(chan struct{})
	defer close(pollWait)
	s := newServerWithStoredGrantAndAdapter(t, &fakeAdapter{
		deviceCode: provider.DeviceCodeResponse{
			VerificationURL: "https://auth.example/device",
			UserCode:        "ABCD-EFGH",
			ExpiresIn:       900,
		},
		pollWait: pollWait,
	})
	handler := s.plaintextMux(listenerConfig{tlsListenAddr: ":8443"})

	token := performRequest(handler, http.MethodGet, "/grants/codex-cluster/token")
	if token.Code != http.StatusForbidden || token.Body.String() != "token endpoint requires mTLS on the SPIFFE port" {
		t.Fatalf("token response = %d %q", token.Code, token.Body.String())
	}

	for _, test := range []struct {
		name, method, path string
	}{
		{"refresh", http.MethodPost, "/grants/codex-cluster/refresh"},
		{"login start", http.MethodPost, "/grants/codex-cluster/login/start"},
		{"login status", http.MethodGet, "/grants/codex-cluster/login/status"},
		{"quota", http.MethodGet, "/quota"},
		{"health", http.MethodGet, "/healthz"},
		{"metrics", http.MethodGet, "/metrics"},
	} {
		t.Run(test.name, func(t *testing.T) {
			response := performRequest(handler, test.method, test.path)
			if response.Code != http.StatusOK {
				t.Fatalf("%s response = %d %s", test.path, response.Code, response.Body.String())
			}
		})
	}
}

func TestPlaintextHandlerServesTokenWhenMTLSDisabled(t *testing.T) {
	s := newServerWithStoredGrant(t)
	response := performRequest(
		s.plaintextMux(listenerConfig{}),
		http.MethodGet,
		"/grants/codex-cluster/token",
	)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"access_token":"stored-access-token"`) {
		t.Fatalf("token response = %d %s", response.Code, response.Body.String())
	}
	if count := testutil.CollectAndCount(s.tokenRequests); count != 0 {
		t.Fatalf("legacy plaintext request emitted %d token request metrics, want 0", count)
	}
}

func TestPlaintextMuxOmitsGitHubGrants(t *testing.T) {
	for _, test := range []struct {
		name      string
		listeners listenerConfig
	}{
		{"legacy", listenerConfig{}},
		{"mtls-enabled", listenerConfig{tlsListenAddr: ":8443"}},
		{"retired", listenerConfig{tlsListenAddr: ":8443", retirePlaintextProtected: true}},
	} {
		t.Run(test.name, func(t *testing.T) {
			handler := newServerWithStoredGrant(t).plaintextMux(test.listeners)
			if health := performRequest(handler, http.MethodGet, "/healthz"); health.Code != http.StatusOK {
				t.Fatalf("health status = %d, want 200", health.Code)
			}
			for _, method := range []string{http.MethodGet, http.MethodPost} {
				for _, path := range []string{"/github/grants/", "/github/grants/bosun/token"} {
					response := performRequest(handler, method, path)
					if response.Code != http.StatusNotFound {
						t.Errorf("%s %s = %d, want 404", method, path, response.Code)
					}
				}
			}
		})
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

	testServer := httptest.NewUnstartedServer(newServerWithStoredGrant(t).mtlsMux(listenerConfig{
		spiffeClientIDs: []spiffeid.ID{allowedID},
	}))
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
	legacyLogin, err := allowedClient.Get(testServer.URL + "/grants/codex-cluster/login/status")
	if err != nil {
		t.Fatalf("legacy login status failed: %v", err)
	}
	legacyLogin.Body.Close()
	if legacyLogin.StatusCode != http.StatusOK {
		t.Fatalf("legacy login status = %d, want 200 with strict login auth disabled", legacyLogin.StatusCode)
	}
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		for _, path := range []string{"/github/grants/", "/github/grants/bosun/token"} {
			request, err := http.NewRequest(method, testServer.URL+path, nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := allowedClient.Do(request)
			if err != nil {
				t.Fatalf("authenticated %s %s failed: %v", method, path, err)
			}
			response.Body.Close()
			if response.StatusCode != http.StatusNotFound {
				t.Errorf("authenticated %s %s = %d, want 404", method, path, response.StatusCode)
			}
		}
	}

	unauthenticatedClient := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsconfig.TLSClientConfig(
		allowedSource,
		tlsconfig.AuthorizeOneOf(serverID),
	)}}
	_, err = unauthenticatedClient.Get(testServer.URL + "/grants/codex-cluster/token")
	if err == nil {
		t.Fatal("client without an X.509-SVID succeeded, want TLS handshake error")
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

	missingClient := &http.Client{Transport: &http.Transport{TLSClientConfig: tlsconfig.TLSClientConfig(
		allowedSource,
		tlsconfig.AuthorizeOneOf(serverID),
	)}}
	_, err = missingClient.Get(testServer.URL + "/grants/codex-cluster/token")
	if err == nil {
		t.Fatal("client without an SVID succeeded, want TLS handshake error")
	}
}

func TestStrictLoginMTLSSeparatesLoginAndTokenIdentities(t *testing.T) {
	trustDomain := spiffeid.RequireTrustDomainFromString("embervm.jomcgi.dev")
	caCertificate, caKey := newTestCA(t)
	bundle := x509bundle.FromX509Authorities(trustDomain, []*x509.Certificate{caCertificate})
	serverID := spiffeid.RequireFromString("spiffe://embervm.jomcgi.dev/ns/embervm-dev/sa/embervm-dev-embervm-tokenbroker")
	tokenID := spiffeid.RequireFromString("spiffe://embervm.jomcgi.dev/ns/embervm-dev/sa/embervm-dev-embervm-noded")
	loginID := spiffeid.RequireFromString("spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith")
	foreignID := spiffeid.RequireFromString("spiffe://embervm.jomcgi.dev/ns/foreign/sa/monolith")
	source := func(id spiffeid.ID, serial int64) *staticX509Source {
		return &staticX509Source{svid: newTestSVID(t, caCertificate, caKey, id, serial), bundle: bundle}
	}
	serverSource := source(serverID, 2)
	listeners := listenerConfig{
		tlsListenAddr:        ":8443",
		spiffeClientIDs:      []spiffeid.ID{tokenID},
		loginAuthRequired:    true,
		loginSpiffeClientIDs: []spiffeid.ID{loginID},
	}
	pollWait := make(chan struct{})
	defer close(pollWait)
	s := newServerWithStoredGrantAndAdapter(t, &fakeAdapter{
		deviceCode: provider.DeviceCodeResponse{
			VerificationURL: "https://auth.example/device",
			UserCode:        "ABCD-EFGH",
			ExpiresIn:       900,
		},
		pollWait: pollWait,
	})
	testServer := httptest.NewUnstartedServer(s.mtlsMux(listeners))
	testServer.TLS = tlsconfig.MTLSServerConfig(serverSource, serverSource, tlsconfig.AuthorizeOneOf(listeners.mtlsClientIDs()...))
	testServer.TLS.Certificates = []tls.Certificate{{
		Certificate: [][]byte{serverSource.svid.Certificates[0].Raw},
		PrivateKey:  serverSource.svid.PrivateKey,
		Leaf:        serverSource.svid.Certificates[0],
	}}
	testServer.StartTLS()
	defer testServer.Close()
	client := func(id spiffeid.ID, serial int64) *http.Client {
		clientSource := source(id, serial)
		transport := &http.Transport{TLSClientConfig: tlsconfig.MTLSClientConfig(clientSource, clientSource, tlsconfig.AuthorizeOneOf(serverID))}
		t.Cleanup(transport.CloseIdleConnections)
		return &http.Client{Transport: transport}
	}
	tokenClient, loginClient := client(tokenID, 3), client(loginID, 4)
	request := func(client *http.Client, method, path string, status int) string {
		t.Helper()
		req, err := http.NewRequest(method, testServer.URL+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		// Identity-like headers must not elevate a token-only client.
		req.Header.Set("X-SPIFFE-ID", loginID.String())
		req.Header.Set("X-Forwarded-Client-Cert", "URI="+loginID.String())
		response, err := client.Do(req)
		if err != nil {
			t.Fatalf("%s %s failed: %v", method, path, err)
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != status {
			t.Fatalf("%s %s = %d %s, want %d", method, path, response.StatusCode, body, status)
		}
		return string(body)
	}
	loginPaths := []struct{ method, path string }{
		{http.MethodPost, "/grants/codex-cluster/login/start"},
		{http.MethodGet, "/grants/codex-cluster/login/status"},
	}
	assertLoginDenied := func() {
		t.Helper()
		for _, route := range loginPaths {
			body := request(tokenClient, route.method, route.path, http.StatusForbidden)
			if strings.Contains(body, "ABCD-EFGH") || !strings.Contains(body, "spiffe_forbidden") {
				t.Fatalf("token identity received login data: %s", body)
			}
			req := httptest.NewRequest(route.method, route.path, nil)
			req.Header.Set("X-SPIFFE-ID", loginID.String())
			response := httptest.NewRecorder()
			s.plaintextMux(listeners).ServeHTTP(response, req)
			if response.Code != http.StatusForbidden || !strings.Contains(response.Body.String(), "login_requires_mtls") {
				t.Fatalf("strict plaintext %s = %d %s", route.path, response.Code, response.Body.String())
			}
		}
	}
	assertLoginDenied()
	request(loginClient, http.MethodGet, "/grants/codex-cluster/login/status", http.StatusOK)
	if body := request(loginClient, http.MethodPost, "/grants/codex-cluster/login/start", http.StatusOK); !strings.Contains(body, "ABCD-EFGH") {
		t.Fatalf("login identity did not receive the device code: %s", body)
	}
	request(loginClient, http.MethodPost, "/grants/codex-cluster/login/start", http.StatusConflict)
	request(loginClient, http.MethodGet, "/grants/codex-cluster/login/status", http.StatusOK)
	assertLoginDenied() // An active pending flow must not expose codes to other callers.
	for _, route := range []struct{ method, path string }{
		{http.MethodGet, "/grants/codex-cluster/token"},
		{http.MethodPost, "/grants/codex-cluster/refresh"},
		{http.MethodGet, "/quota"},
	} {
		request(loginClient, route.method, route.path, http.StatusForbidden)
		request(tokenClient, route.method, route.path, http.StatusOK)
	}
	request(loginClient, http.MethodPost, "/quota/codex", http.StatusForbidden)

	noCertTransport := &http.Transport{TLSClientConfig: tlsconfig.TLSClientConfig(serverSource, tlsconfig.AuthorizeOneOf(serverID))}
	t.Cleanup(noCertTransport.CloseIdleConnections)
	for name, rejected := range map[string]*http.Client{
		"foreign":        client(foreignID, 5),
		"no certificate": {Transport: noCertTransport},
	} {
		for _, path := range []string{"/grants/codex-cluster/login/status", "/grants/codex-cluster/token"} {
			response, err := rejected.Get(testServer.URL + path)
			if err == nil {
				response.Body.Close()
				t.Errorf("%s request %s passed TLS, want handshake refusal", name, path)
			}
		}
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
	return newServerWithStoredGrantAndAdapter(t, &fakeAdapter{deviceCode: provider.DeviceCodeResponse{}})
}

func newServerWithStoredGrantAndAdapter(t *testing.T, adapter *fakeAdapter) *server {
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
		adapter,
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
