package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
)

type githubTestMinter struct {
	calls int
	err   error
}

func (m *githubTestMinter) Mint(context.Context) (provider.TokenResponse, error) {
	m.calls++
	return provider.TokenResponse{AccessToken: "test-installation-token", ExpiresAt: time.Now().Add(time.Hour)}, m.err
}

func TestGitHubGrantAuthorizesEachCallerBeforeMinting(t *testing.T) {
	for _, tc := range []struct {
		name, caller, path string
		tls                bool
		status             int
	}{
		{"authorized", "publisher", "/github/grants/bosun-publish/token", true, 200},
		{"other-trusted-service", "implementer", "/github/grants/bosun-publish/token", true, 403},
		{"anonymous", "", "/github/grants/bosun-publish/token", true, 403},
		{"plaintext", "publisher", "/github/grants/bosun-publish/token", false, 403},
		{"scope-override", "publisher", "/github/grants/bosun-publish/token?profile=merger", true, 404},
		{"unknown-grant", "publisher", "/github/grants/unknown/token", true, 404},
	} {
		t.Run(tc.name, func(t *testing.T) {
			minter := &githubTestMinter{}
			s := &server{logger: slog.New(slog.NewTextHandler(io.Discard, nil)), githubGrants: map[string]githubGrant{
				"bosun-publish": {minter: minter, profile: "review-publisher", callers: map[string]bool{"spiffe://test.example/publisher": true}},
			}}
			r := httptest.NewRequest(http.MethodGet, tc.path, nil)
			// A forged header is never considered authority.
			r.Header.Set("X-SPIFFE-ID", "spiffe://test.example/publisher")
			r.Header.Set("X-Factory-Role", "review-publisher")
			if tc.caller != "" {
				u, _ := url.Parse("spiffe://test.example/" + tc.caller)
				r.TLS = &tls.ConnectionState{PeerCertificates: []*x509.Certificate{{URIs: []*url.URL{u}}}}
			}
			w := httptest.NewRecorder()
			s.githubHandler(tc.tls)(w, r)
			if w.Code != tc.status {
				t.Fatalf("status %d, want %d: %s", w.Code, tc.status, w.Body.String())
			}
			if tc.status == 200 {
				if minter.calls != 1 || !strings.Contains(w.Body.String(), "test-installation-token") {
					t.Fatal("authorized token not returned")
				}
			} else if minter.calls != 0 || strings.Contains(w.Body.String(), "test-installation-token") {
				t.Fatal("unauthorized mint")
			}
			if w.Header().Get("Cache-Control") != "no-store" {
				t.Fatal("token response can be cached")
			}
		})
	}
}

func TestGitHubMintFailureDoesNotReturnToken(t *testing.T) {
	minter := &githubTestMinter{err: errors.New("upstream unavailable")}
	s := &server{logger: slog.New(slog.NewTextHandler(io.Discard, nil)), githubGrants: map[string]githubGrant{
		"bosun": {minter: minter, callers: map[string]bool{"spiffe://test.example/service": true}},
	}}
	u, _ := url.Parse("spiffe://test.example/service")
	r := httptest.NewRequest("GET", "/github/grants/bosun/token", nil)
	r.TLS = &tls.ConnectionState{PeerCertificates: []*x509.Certificate{{URIs: []*url.URL{u}}}}
	w := httptest.NewRecorder()
	s.githubHandler(true)(w, r)
	if w.Code != 502 || strings.Contains(w.Body.String(), "test-installation-token") {
		t.Fatalf("unsafe failure: %s", w.Body.String())
	}
}

func TestConfiguredGitHubGrantsRequireExplicitScopeAndCallers(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("GITHUB_APP_ID", "123")
	t.Setenv("GITHUB_APP_INSTALLATION_ID", "456")
	t.Setenv("GITHUB_APP_PRIVATE_KEY", string(pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})))
	caller := "spiffe://test.example/publisher"
	listeners := listenerConfig{tlsListenAddr: ":8443", spiffeClientIDs: []spiffeid.ID{spiffeid.RequireFromString(caller)}}
	valid := githubGrantConfig{Name: "bosun-publisher", Profile: "review-publisher", RepositoryIDs: []int64{789}, AllowedSPIFFEIDs: []string{caller}}
	for _, kind := range []string{"valid", "no-tls", "no-callers", "unknown-caller", "no-repositories", "unknown-profile", "duplicate", "invalid-name"} {
		t.Run(kind, func(t *testing.T) {
			c := valid
			l := listeners
			switch kind {
			case "no-tls":
				l.tlsListenAddr = ""
			case "no-callers":
				c.AllowedSPIFFEIDs = nil
			case "unknown-caller":
				c.AllowedSPIFFEIDs = []string{"spiffe://test.example/other"}
			case "no-repositories":
				c.RepositoryIDs = nil
			case "unknown-profile":
				c.Profile = "admin"
			case "invalid-name":
				c.Name = "../escape"
			}
			configs := []githubGrantConfig{c}
			if kind == "duplicate" {
				configs = append(configs, c)
			}
			raw, _ := json.Marshal(configs)
			t.Setenv("GITHUB_APP_GRANTS", string(raw))
			grants, err := configuredGitHubGrants(l)
			if kind == "valid" {
				if err != nil || len(grants) != 1 {
					t.Fatalf("valid config: %v", err)
				}
			} else if err == nil {
				t.Fatal("accepted unsafe configuration")
			}
		})
	}
}

func TestGitHubDisabledNeedsNoCredentials(t *testing.T) {
	t.Setenv("GITHUB_APP_GRANTS", "")
	t.Setenv("GITHUB_APP_PRIVATE_KEY", "")
	grants, err := configuredGitHubGrants(listenerConfig{})
	if err != nil || len(grants) != 0 {
		t.Fatalf("disabled: %v", err)
	}
}
