package authentik

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestMint(t *testing.T) {
	const expiresIn = 300
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
			return
		}
		if got := r.Header.Get("Content-Type"); got != "application/x-www-form-urlencoded" {
			t.Errorf("Content-Type = %q", got)
			return
		}
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse form: %v", err)
			return
		}
		if got := r.Form.Get("grant_type"); got != "client_credentials" {
			t.Errorf("grant_type = %q", got)
		}
		if got := r.Form.Get("client_id"); got != "test-client" {
			t.Errorf("client_id = %q", got)
		}
		if got := r.Form.Get("username"); got != "test-service-account" {
			t.Errorf("username was not sent, got %q", got)
		}
		if got := r.Form.Get("password"); got != "test-app-password" {
			t.Errorf("app password was not sent")
		}
		if r.Form.Has("client_secret") {
			t.Errorf("client_secret must not be sent: authentik M2M uses an app password")
		}
		if got := r.Form.Get("scope"); got != "openid profile" {
			t.Errorf("scope = %q", got)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"access_token": "minted-token", "expires_in": expiresIn})
	}))
	defer server.Close()

	adapter := &Adapter{HTTPClient: server.Client(), TokenEndpoint: server.URL, ClientID: "test-client", Username: "test-service-account", AppPassword: "test-app-password", Scope: "openid profile"}
	before := time.Now()
	token, err := adapter.Mint(context.Background())
	after := time.Now()
	if err != nil {
		t.Fatal(err)
	}
	if token.AccessToken != "minted-token" {
		t.Fatal("AccessToken does not match token response")
	}
	if token.ExpiresAt.Before(before.Add(expiresIn*time.Second)) || token.ExpiresAt.After(after.Add(expiresIn*time.Second)) {
		t.Fatalf("ExpiresAt = %v, want time.Now() + %s", token.ExpiresAt, expiresIn*time.Second)
	}
}

func TestMintNon2xxDoesNotExposeSecret(t *testing.T) {
	const secret = "never-print-this-secret"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":"invalid_client","detail":"never-print-this-secret"}`))
	}))
	defer server.Close()

	adapter := &Adapter{HTTPClient: server.Client(), TokenEndpoint: server.URL, AppPassword: secret}
	_, err := adapter.Mint(context.Background())
	if err == nil {
		t.Fatal("Mint returned no error")
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatal("error exposed client secret")
	}
	if !strings.Contains(err.Error(), "401 Unauthorized") {
		t.Fatalf("error = %v, want clear status error", err)
	}
}

func TestMintMalformedResponseDoesNotExposeSecret(t *testing.T) {
	const secret = "never-print-this-secret"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"access_token":"never-print-this-secret"`))
	}))
	defer server.Close()

	adapter := &Adapter{HTTPClient: server.Client(), TokenEndpoint: server.URL, AppPassword: secret}
	_, err := adapter.Mint(context.Background())
	if err == nil {
		t.Fatal("Mint returned no error")
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatal("error exposed client secret")
	}
	if !strings.Contains(err.Error(), "decode token response") {
		t.Fatalf("error = %v, want clear decode error", err)
	}
}
