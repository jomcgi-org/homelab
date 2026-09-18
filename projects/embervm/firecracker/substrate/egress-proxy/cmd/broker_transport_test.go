package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
)

type staticBrokerSource struct {
	svid   *x509svid.SVID
	bundle *x509bundle.Bundle
	closed bool
}

func (s *staticBrokerSource) GetX509SVID() (*x509svid.SVID, error) {
	if s.svid == nil {
		return nil, errors.New("no SVID")
	}
	return s.svid, nil
}

func (s *staticBrokerSource) GetX509BundleForTrustDomain(spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	return s.bundle, nil
}
func (s *staticBrokerSource) Close() error { s.closed = true; return nil }

func TestBrokerTransportConfigurationFailsClosed(t *testing.T) {
	for _, c := range []struct{ url, id string }{
		{"http://broker:8080", "spiffe://test.example/broker"},
		{"broker:8443", "spiffe://test.example/broker"},
		{"https://broker:8443", ""},
		{"https://broker:8443", "invalid"},
		{"", "spiffe://test.example/broker"},
		{"http://user:password@broker", ""},
		{"http://broker/path", ""},
		{"http://broker?query=1", ""},
		{"http://broker#fragment", ""},
	} {
		t.Run(c.url+"/"+c.id, func(t *testing.T) {
			_, _, err := brokerTransport(c.url, c.id, func(context.Context) (brokerX509Source, error) {
				t.Fatal("invalid config attempted SPIFFE startup")
				return nil, nil
			})
			if err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
	_, _, err := brokerTransport("https://broker:8443", "spiffe://test.example/broker", func(ctx context.Context) (brokerX509Source, error) {
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 30*time.Second {
			t.Fatal("startup is not bounded")
		}
		return nil, context.DeadlineExceeded
	})
	if err == nil {
		t.Fatal("source failure accepted")
	}
	source := &staticBrokerSource{}
	_, _, err = brokerTransport("https://broker:8443", "spiffe://test.example/broker", func(context.Context) (brokerX509Source, error) { return source, nil })
	if err == nil || !source.closed {
		t.Fatal("missing SVID did not fail and close source")
	}
}

func TestBrokerMTLSAuthenticatesAllOperations(t *testing.T) {
	domain := spiffeid.RequireTrustDomainFromString("test.example")
	ca, key := newTestCA(t)
	bundle := x509bundle.FromX509Authorities(domain, []*x509.Certificate{ca})
	serverID := spiffeid.RequireFromString("spiffe://test.example/broker")
	clientID := spiffeid.RequireFromString("spiffe://test.example/noded")
	serverSource := &staticBrokerSource{svid: newTestSVID(t, ca, key, serverID, 2), bundle: bundle}
	clientSource := &staticBrokerSource{svid: newTestSVID(t, ca, key, clientID, 3), bundle: bundle}
	var calls atomic.Int32
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id, err := x509svid.IDFromCert(r.TLS.PeerCertificates[0])
		if err != nil || id != clientID {
			t.Error("missing authenticated noded identity")
		}
		calls.Add(1)
		switch r.URL.Path {
		case "/grants/codex/token":
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "test-token", "expires_at": time.Now().Add(time.Hour)})
		case "/quota":
			_, _ = io.WriteString(w, `{"grants":{}}`)
		default:
			w.WriteHeader(http.StatusOK)
		}
	}))
	server.TLS = tlsconfig.MTLSServerConfig(serverSource, serverSource, tlsconfig.AuthorizeID(clientID))
	server.TLS.Certificates = []tls.Certificate{{Certificate: [][]byte{serverSource.svid.Certificates[0].Raw}, PrivateKey: serverSource.svid.PrivateKey}}
	server.StartTLS()
	defer server.Close()
	factory := func(context.Context) (brokerX509Source, error) { return clientSource, nil }
	transport, closeTransport, err := brokerTransport(server.URL, serverID.String(), factory)
	if err != nil {
		t.Fatal(err)
	}
	defer closeTransport()
	client := brokerHTTPClient(transport, time.Second)
	broker := newTokenBrokerWithClient(server.URL, client)
	token, _, err := broker.token("codex")
	if err != nil || token != "test-token" {
		t.Fatalf("token fetch: %v", err)
	}
	if err := broker.forceRefresh("codex"); err != nil {
		t.Fatal(err)
	}
	if broker.fetchGrantViews() == nil {
		t.Fatal("quota read failed")
	}
	reporter := newQuotaReporterWithClient(server.URL, slog.New(slog.NewTextHandler(io.Discard, nil)), client)
	reporter.post(QuotaObservation{Provider: "codex", Grant: "codex"})
	if calls.Load() != 4 {
		t.Fatalf("got %d authenticated calls, want 4", calls.Load())
	}

	wrongTransport, closeWrong, err := brokerTransport(server.URL, "spiffe://test.example/impostor", factory)
	if err != nil {
		t.Fatal(err)
	}
	defer closeWrong()
	if response, err := brokerHTTPClient(wrongTransport, time.Second).Get(server.URL + "/quota"); err == nil {
		response.Body.Close()
		t.Fatal("accepted wrong broker identity")
	}
	// An attacker with a valid SVID in the same trust domain is still denied.
	intruder := &staticBrokerSource{svid: newTestSVID(t, ca, key, spiffeid.RequireFromString("spiffe://test.example/intruder"), 4), bundle: bundle}
	intruderTransport, closeIntruder, err := brokerTransport(server.URL, serverID.String(), func(context.Context) (brokerX509Source, error) { return intruder, nil })
	if err != nil {
		t.Fatal(err)
	}
	defer closeIntruder()
	if response, err := brokerHTTPClient(intruderTransport, time.Second).Get(server.URL + "/quota"); err == nil {
		response.Body.Close()
		t.Fatal("accepted wrong caller identity")
	}
	if calls.Load() != 4 {
		t.Fatal("unauthorized request reached broker handler")
	}
	if response, err := client.Get("http://" + server.Listener.Addr().String() + "/quota"); err == nil {
		response.Body.Close()
		t.Fatal("allowed downgrade")
	}
}

func TestBrokerLegacyTransportRejectsRedirects(t *testing.T) {
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { targetCalls.Add(1) }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	transport, cleanup, err := brokerTransport(server.URL, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	client := brokerHTTPClient(transport, time.Second)
	response, err := client.Get(server.URL + "/grants/test/token")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusTemporaryRedirect || targetCalls.Load() != 0 {
		t.Fatal("followed broker redirect")
	}
	if response, err := client.Get(target.URL); err == nil {
		response.Body.Close()
		t.Fatal("allowed another origin")
	}
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
