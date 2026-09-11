// Package githubapp mints explicitly scoped installation tokens for one App.
package githubapp

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
)

const apiURL = "https://api.github.com"

// Permissions returns a new map: callers cannot mutate another profile.
// These are GitHub permission sets, not branch or operation restrictions.
func Permissions(profile string) (map[string]string, error) {
	p := map[string]string{"contents": "read", "pull_requests": "read", "metadata": "read"}
	switch profile {
	case "planner":
		p["issues"] = "write"
	case "implementer":
		p["contents"], p["pull_requests"], p["issues"] = "write", "write", "write"
		p["checks"], p["statuses"] = "read", "read"
	case "reviewer":
		p["checks"], p["statuses"] = "read", "read"
	case "review-publisher":
		p["checks"] = "write"
	case "merger":
		p["contents"], p["checks"], p["statuses"] = "write", "read", "read"
	default:
		return nil, fmt.Errorf("unknown GitHub permission profile %q", profile)
	}
	return p, nil
}

func ParsePrivateKey(raw []byte) (*rsa.PrivateKey, error) {
	block, rest := pem.Decode(raw)
	if block == nil || len(bytes.TrimSpace(rest)) != 0 {
		return nil, errors.New("GitHub App key must contain one PEM private key")
	}
	var key *rsa.PrivateKey
	switch block.Type {
	case "RSA PRIVATE KEY":
		parsed, err := x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil, errors.New("invalid GitHub App RSA private key")
		}
		key = parsed
	case "PRIVATE KEY":
		parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, errors.New("invalid GitHub App PKCS8 private key")
		}
		key, _ = parsed.(*rsa.PrivateKey)
	}
	if key == nil || key.N.BitLen() < 2048 {
		return nil, errors.New("GitHub App requires an RSA key of at least 2048 bits")
	}
	return key, key.Validate()
}

// Adapter caches only in memory. Restarting after a scope/configuration change
// cannot reuse a persisted token minted with the previous, broader permissions.
// A grant is shared by its authorized service callers, not a session identity.
type Adapter struct {
	appID, installationID int64
	key                   *rsa.PrivateKey
	repositories          []int64
	permissions           map[string]string
	client                *http.Client
	mu                    sync.Mutex
	cached                provider.TokenResponse
}

var _ provider.Minter = (*Adapter)(nil)

func New(appID, installationID int64, key *rsa.PrivateKey, repositories []int64, profile string) (*Adapter, error) {
	if appID <= 0 || installationID <= 0 || key == nil {
		return nil, errors.New("GitHub App ID, installation ID and private key are required")
	}
	if len(repositories) == 0 || len(repositories) > 500 {
		return nil, errors.New("GitHub grant requires 1 to 500 explicit repository IDs")
	}
	seen := map[int64]bool{}
	for _, id := range repositories {
		if id <= 0 || seen[id] {
			return nil, errors.New("GitHub repository IDs must be positive and unique")
		}
		seen[id] = true
	}
	permissions, err := Permissions(profile)
	if err != nil {
		return nil, err
	}
	return &Adapter{
		appID: appID, installationID: installationID, key: key,
		repositories: append([]int64(nil), repositories...), permissions: permissions,
		client: &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
	}, nil
}

func (a *Adapter) jwt(now time.Time) (string, error) {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	claims, err := json.Marshal(map[string]any{
		"iss": strconv.FormatInt(a.appID, 10), "iat": now.Add(-time.Minute).Unix(),
		"exp": now.Add(9 * time.Minute).Unix(),
	})
	if err != nil {
		return "", err
	}
	unsigned := header + "." + base64.RawURLEncoding.EncodeToString(claims)
	digest := sha256.Sum256([]byte(unsigned))
	signature, err := rsa.SignPKCS1v15(rand.Reader, a.key, crypto.SHA256, digest[:])
	if err != nil {
		return "", errors.New("could not sign GitHub App JWT")
	}
	return unsigned + "." + base64.RawURLEncoding.EncodeToString(signature), nil
}

func (a *Adapter) Mint(ctx context.Context) (provider.TokenResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return provider.TokenResponse{}, err
	}
	if a.cached.AccessToken != "" && time.Until(a.cached.ExpiresAt) > time.Minute {
		return a.cached, nil
	}
	var out provider.TokenResponse
	token, err := a.jwt(time.Now())
	if err != nil {
		return out, err
	}
	body, err := json.Marshal(map[string]any{
		"repository_ids": a.repositories, "permissions": a.permissions,
	})
	if err != nil {
		return out, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		fmt.Sprintf("%s/app/installations/%d/access_tokens", apiURL, a.installationID), bytes.NewReader(body))
	if err != nil {
		return out, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Api-Version", "2026-03-10")
	resp, err := a.client.Do(req)
	if err != nil {
		return out, errors.New("GitHub installation token request failed")
	}
	defer resp.Body.Close()
	// Do not reflect response bodies: they can contain tokens or upstream echoes.
	if resp.StatusCode != http.StatusCreated {
		return out, fmt.Errorf("GitHub installation token request returned HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1_000_001))
	if err != nil || len(data) > 1_000_000 {
		return out, errors.New("could not read bounded GitHub token response")
	}
	var result struct {
		Token       string            `json:"token"`
		ExpiresAt   time.Time         `json:"expires_at"`
		Permissions map[string]string `json:"permissions"`
	}
	if json.Unmarshal(data, &result) != nil || result.Token == "" || time.Until(result.ExpiresAt) <= time.Minute {
		return out, errors.New("invalid GitHub installation token response")
	}
	if len(result.Permissions) == 0 {
		return out, errors.New("GitHub installation token response omitted permissions")
	}
	for permission, level := range result.Permissions {
		requested, ok := a.permissions[permission]
		if !ok || (level != "read" && level != "write") || (level == "write" && requested != "write") {
			return out, errors.New("GitHub returned permissions outside the requested profile")
		}
	}
	out.AccessToken, out.ExpiresAt = result.Token, result.ExpiresAt
	a.cached = out
	return out, nil
}
