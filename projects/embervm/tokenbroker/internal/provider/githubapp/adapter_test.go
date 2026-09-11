package githubapp

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func testAdapter(t *testing.T, profile string) *Adapter {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	a, err := New(123, 456, key, []int64{789}, profile)
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func tokenResponse(permissions map[string]string) *http.Response {
	body, _ := json.Marshal(map[string]any{
		"token":      "ghs_opaque_token_not_assumed_to_be_40_characters",
		"expires_at": time.Now().Add(time.Hour).UTC(), "permissions": permissions,
	})
	return &http.Response{StatusCode: http.StatusCreated, Body: io.NopCloser(strings.NewReader(string(body))), Header: http.Header{}}
}

func TestMintScopesRepositoryPermissionsAndSignsJWT(t *testing.T) {
	a := testAdapter(t, "implementer")
	calls := 0
	a.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.URL.String() != "https://api.github.com/app/installations/456/access_tokens" || r.Method != "POST" {
			t.Fatalf("unexpected token destination %s %s", r.Method, r.URL)
		}
		var body struct {
			RepositoryIDs []int64           `json:"repository_ids"`
			Permissions   map[string]string `json:"permissions"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if len(body.RepositoryIDs) != 1 || body.RepositoryIDs[0] != 789 || body.Permissions["contents"] != "write" || body.Permissions["checks"] != "read" {
			t.Fatalf("incorrect token scope: %+v", body)
		}
		parts := strings.Split(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), ".")
		if len(parts) != 3 {
			t.Fatal("expected signed JWT")
		}
		sig, err := base64.RawURLEncoding.DecodeString(parts[2])
		if err != nil {
			t.Fatal(err)
		}
		digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
		if err := rsa.VerifyPKCS1v15(&a.key.PublicKey, crypto.SHA256, digest[:], sig); err != nil {
			t.Fatal(err)
		}
		claimsRaw, _ := base64.RawURLEncoding.DecodeString(parts[1])
		var claims struct {
			Iss      string
			Iat, Exp int64
		}
		if err := json.Unmarshal(claimsRaw, &claims); err != nil {
			t.Fatal(err)
		}
		now := time.Now().Unix()
		if claims.Iss != "123" || claims.Iat > now || claims.Iat < now-65 || claims.Exp <= now || claims.Exp > now+600 {
			t.Fatalf("invalid JWT claims: %+v", claims)
		}
		return tokenResponse(body.Permissions), nil
	})
	// Concurrent callers share one scoped mint, not a mint per request.
	var wg sync.WaitGroup
	for range 5 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out, err := a.Mint(context.Background())
			if err != nil || out.AccessToken == "" {
				t.Errorf("Mint = %+v, %v", out, err)
			}
		}()
	}
	wg.Wait()
	if calls != 1 {
		t.Fatalf("mint calls = %d, want 1", calls)
	}
	a.cached.ExpiresAt = time.Now().Add(30 * time.Second)
	if _, err := a.Mint(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatal("near-expiry token was reused")
	}
}

func TestOnlyPublisherCanWriteChecks(t *testing.T) {
	for _, profile := range []string{"planner", "implementer", "reviewer", "review-publisher", "merger"} {
		p, err := Permissions(profile)
		if err != nil {
			t.Fatal(err)
		}
		if (p["checks"] == "write") != (profile == "review-publisher") {
			t.Fatalf("profile %s can publish checks", profile)
		}
		if profile == "reviewer" {
			for name, level := range p {
				if level != "read" {
					t.Fatalf("reviewer can write %s", name)
				}
			}
		}
	}
	p, _ := Permissions("reviewer")
	p["checks"] = "write"
	fresh, _ := Permissions("reviewer")
	if fresh["checks"] != "read" {
		t.Fatal("profiles share mutable permissions")
	}
}

func TestRejectsUnscopedConfigurationAndBadKeys(t *testing.T) {
	a := testAdapter(t, "reviewer")
	for _, ids := range [][]int64{nil, {}, {0}, {1, 1}, {-1}, make([]int64, 501)} {
		if _, err := New(123, 456, a.key, ids, "reviewer"); err == nil {
			t.Fatalf("accepted repositories %v", ids)
		}
	}
	if _, err := New(123, 456, a.key, []int64{789}, "admin"); err == nil {
		t.Fatal("accepted unknown profile")
	}
	if _, err := New(0, 456, a.key, []int64{789}, "reviewer"); err == nil {
		t.Fatal("accepted missing App ID")
	}
	if _, err := ParsePrivateKey([]byte("not a key")); err == nil {
		t.Fatal("accepted malformed key")
	}
	for _, kind := range []string{"RSA PRIVATE KEY", "PRIVATE KEY"} {
		der := x509.MarshalPKCS1PrivateKey(a.key)
		if kind == "PRIVATE KEY" {
			der, _ = x509.MarshalPKCS8PrivateKey(a.key)
		}
		raw := pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der})
		parsed, err := ParsePrivateKey(raw)
		if err != nil || parsed.N.Cmp(a.key.N) != 0 {
			t.Fatalf("key format %s: %v", kind, err)
		}
	}
}

func TestUpstreamErrorsNeverReturnOrCacheTokens(t *testing.T) {
	for _, kind := range []string{"error", "redirect", "expired", "empty", "missing_permissions", "elevated", "oversized", "malformed"} {
		t.Run(kind, func(t *testing.T) {
			a := testAdapter(t, "reviewer")
			a.client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				if r.URL.Host != "api.github.com" {
					t.Fatal("followed token redirect")
				}
				response := tokenResponse(a.permissions)
				switch kind {
				case "error":
					response.StatusCode = 403
				case "redirect":
					response.StatusCode = 307
					response.Header.Set("Location", "https://attacker.example/token")
				case "elevated":
					response = tokenResponse(map[string]string{"checks": "write"})
				default:
					body := `{"token":"secret-canary","expires_at":"2000-01-01T00:00:00Z","permissions":{"contents":"read"}}`
					if kind == "empty" {
						body = `{}`
					}
					if kind == "missing_permissions" {
						body = fmt.Sprintf(`{"token":"secret-canary","expires_at":%q}`, time.Now().Add(time.Hour).Format(time.RFC3339))
					}
					if kind == "oversized" {
						body = strings.Repeat("x", 1_000_001)
					}
					if kind == "malformed" {
						body = `{"token":"secret-canary",`
					}
					response.Body = io.NopCloser(strings.NewReader(body))
				}
				return response, nil
			})
			out, err := a.Mint(context.Background())
			if err == nil || out.AccessToken != "" || a.cached.AccessToken != "" || strings.Contains(err.Error(), "secret-canary") {
				t.Fatalf("unsafe failure: %+v, %v", out, err)
			}
		})
	}
}
