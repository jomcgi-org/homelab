package main

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	jose "github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

func TestBearerVerification(t *testing.T) {
	key := func() *rsa.PrivateKey {
		k, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			t.Fatal(err)
		}
		return k
	}
	trusted, untrusted := key(), key()
	keys := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &trusted.PublicKey, KeyID: "active", Algorithm: "RS256", Use: "sig"}}}
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/jwks/" {
			http.NotFound(w, r)
			return
		}
		if err := json.NewEncoder(w).Encode(keys); err != nil {
			t.Error(err)
		}
	}))
	defer endpoint.Close()
	for _, tc := range []struct {
		name, iss, aud string
		ttl            time.Duration
		key            *rsa.PrivateKey
		status         int
	}{
		{"long lifetime", issuer, audience, time.Hour, trusted, 401},
		{"missing username", issuer, audience, 10 * time.Minute, trusted, 401},
		{"valid", issuer, audience, 10 * time.Minute, trusted, 200},
		{"invalid signature", issuer, audience, 10 * time.Minute, untrusted, 401},
		{"wrong issuer", issuer + "other", audience, 10 * time.Minute, trusted, 401},
		{"wrong audience", issuer, "factory-kubectl", 10 * time.Minute, trusted, 401},
		{"expired", issuer, audience, -time.Minute, trusted, 401},
	} {
		t.Run(tc.name, func(t *testing.T) {
			signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: tc.key}, new(jose.SignerOptions).WithHeader("kid", "active"))
			if err != nil {
				t.Fatal(err)
			}
			now := time.Now()
			name := "visitor"
			if tc.name == "missing username" {
				name = ""
			}
			raw, err := jwt.Signed(signer).Claims(jwt.Claims{Subject: "visitor-123", Issuer: tc.iss, Audience: jwt.Audience{tc.aud}, IssuedAt: jwt.NewNumericDate(now), Expiry: jwt.NewNumericDate(now.Add(tc.ttl))}).Claims(map[string]string{"preferred_username": name}).Serialize()
			if err != nil {
				t.Fatal(err)
			}
			p := newTestProxy()
			if w := request(p, "GET", "/api", "warm"); w.Code != 200 {
				t.Fatal(w.Code)
			}
			p.subject = (&verifier{jwksURL: endpoint.URL + "/jwks/"}).subject
			w := request(p, "GET", "/api", raw)
			if w.Code != tc.status {
				t.Fatalf("status = %d, want %d", w.Code, tc.status)
			}
			if tc.status == 200 && p.users["visitor-123"] == nil {
				t.Fatal("verified subject has no window")
			}
			if tc.status == 401 && len(p.users) != 1 {
				t.Fatal("invalid bearer opened a window")
			}
		})
	}
}
