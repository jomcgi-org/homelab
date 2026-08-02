package codexchatgpt

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDeviceFlowAndExchange(t *testing.T) {
	polls := 0
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/accounts/deviceauth/usercode":
			json.NewEncoder(w).Encode(map[string]any{"device_auth_id": "id", "user_code": "code", "interval": 1, "expires_in": 900})
		case "/api/accounts/deviceauth/token":
			polls++
			if polls < 2 {
				w.WriteHeader(http.StatusForbidden)
				return
			}
			json.NewEncoder(w).Encode(map[string]string{"authorization_code": "auth", "code_verifier": "verifier"})
		case "/oauth/token":
			if err := r.ParseForm(); err != nil {
				t.Fatal(err)
			}
			if r.Form.Get("grant_type") != "authorization_code" || r.Form.Get("code_verifier") != "verifier" {
				t.Fatalf("bad exchange form: %v", r.Form)
			}
			json.NewEncoder(w).Encode(map[string]string{"id_token": "id-token", "access_token": "access", "refresh_token": "refresh"})
		}
	}))
	defer ts.Close()
	c := &Adapter{Issuer: ts.URL, HTTPClient: ts.Client()}
	device, err := c.StartDeviceFlow(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	auth, err := c.PollForAuthorization(context.Background(), device)
	if err != nil {
		t.Fatal(err)
	}
	tok, err := c.ExchangeCode(context.Background(), auth)
	if err != nil || tok.RefreshToken != "refresh" {
		t.Fatalf("exchange = %+v, %v", tok, err)
	}
}

func TestRefreshTokenReused(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "refresh_token_reused"})
	}))
	defer ts.Close()
	c := &Adapter{Issuer: ts.URL, HTTPClient: ts.Client()}
	_, err := c.RefreshToken(context.Background(), "old")
	if err == nil || !strings.Contains(err.Error(), "refresh_token_reused") {
		t.Fatalf("error = %v", err)
	}
}
