package fakes

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"math/big"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// IdP fakes Substrate's ate-idp-server: the discovery document and the key
// set, over TLS, with keys that can be swapped to model rotation.
type IdP struct {
	*httptest.Server
	mu        sync.Mutex
	keys      []map[string]string
	jwksCalls int
}

func NewIdP(t *testing.T) *IdP {
	t.Helper()
	idp := &IdP{}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /.well-known/openid-configuration", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"issuer": idp.URL, "jwks_uri": idp.URL + "/openid/v1/jwks"})
	})
	mux.HandleFunc("GET /openid/v1/jwks", func(w http.ResponseWriter, r *http.Request) {
		idp.mu.Lock()
		defer idp.mu.Unlock()
		idp.jwksCalls++
		writeJSON(w, http.StatusOK, map[string]any{"keys": idp.keys})
	})
	idp.Server = httptest.NewTLSServer(mux)
	t.Cleanup(idp.Close)
	return idp
}

// Publish replaces the key set.
func (idp *IdP) Publish(signers ...Signer) {
	keys := make([]map[string]string, 0, len(signers))
	for _, s := range signers {
		keys = append(keys, s.JWK())
	}
	idp.mu.Lock()
	idp.keys = keys
	idp.mu.Unlock()
}

func (idp *IdP) JWKSCalls() int {
	idp.mu.Lock()
	defer idp.mu.Unlock()
	return idp.jwksCalls
}

// Signer signs actor JWTs with an ES256 or RS256 key.
type Signer struct {
	Kid string
	EC  *ecdsa.PrivateKey
	RSA *rsa.PrivateKey
}

func NewECSigner(t *testing.T, kid string) Signer {
	t.Helper()
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return Signer{Kid: kid, EC: k}
}

func NewRSASigner(t *testing.T, kid string) Signer {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return Signer{Kid: kid, RSA: k}
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func (s Signer) JWK() map[string]string {
	if s.RSA != nil {
		return map[string]string{"kty": "RSA", "kid": s.Kid, "use": "sig", "alg": "RS256", "n": b64(s.RSA.N.Bytes()), "e": b64(big.NewInt(int64(s.RSA.E)).Bytes())}
	}
	return map[string]string{
		"kty": "EC", "kid": s.Kid, "use": "sig", "alg": "ES256", "crv": "P-256",
		"x": b64(s.EC.X.FillBytes(make([]byte, 32))), "y": b64(s.EC.Y.FillBytes(make([]byte, 32))),
	}
}

// Sign returns a compact JWT over claims.
func (s Signer) Sign(t *testing.T, claims map[string]any) string {
	t.Helper()
	alg := "ES256"
	if s.RSA != nil {
		alg = "RS256"
	}
	h, _ := json.Marshal(map[string]string{"typ": "JWT", "alg": alg, "kid": s.Kid})
	c, _ := json.Marshal(claims)
	input := b64(h) + "." + b64(c)
	digest := sha256.Sum256([]byte(input))
	var sig []byte
	if s.RSA != nil {
		var err error
		if sig, err = rsa.SignPKCS1v15(rand.Reader, s.RSA, crypto.SHA256, digest[:]); err != nil {
			t.Fatal(err)
		}
	} else {
		r, ss, err := ecdsa.Sign(rand.Reader, s.EC, digest[:])
		if err != nil {
			t.Fatal(err)
		}
		sig = append(r.FillBytes(make([]byte, 32)), ss.FillBytes(make([]byte, 32))...)
	}
	return input + "." + b64(sig)
}

// ActorClaims are the claims Substrate mints for an actor, valid at now.
func ActorClaims(issuer, audience, atespace, actor string, now time.Time) map[string]any {
	return map[string]any{
		"iss": issuer, "sub": "actor/" + atespace + "/" + actor, "aud": []string{audience},
		"exp": now.Add(15 * time.Minute).Unix(), "nbf": now.Add(-5 * time.Minute).Unix(), "iat": now.Unix(), "jti": "jti-1",
		"actor.ate.dev": map[string]string{"atespace": atespace, "actorName": actor, "actorUID": "uid-" + actor},
	}
}
