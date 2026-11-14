// Package github mints GitHub App installation tokens downscoped to one
// repository: agent tokens written to each binding's git Secret, and
// connector tokens served on request.
package github

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
	"os"
	"strconv"
	"strings"
	"time"
)

// App authenticates as a GitHub App installation.
type App struct {
	APIURL         string
	AppID          string
	InstallationID string
	Key            *rsa.PrivateKey
	Client         *http.Client
	Now            func() time.Time
}

// Token is an installation token and what GitHub granted it.
type Token struct {
	Token       string
	ExpiresAt   time.Time
	Repository  string // owner/name
	Permissions map[string]string
}

// LoadPrivateKey reads the App's PEM private key (PKCS#1 or PKCS#8).
func LoadPrivateKey(path string) (*rsa.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read GitHub App key: %w", err)
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("GitHub App key is not PEM")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse GitHub App key: %w", err)
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("GitHub App key is not RSA")
	}
	return key, nil
}

func (a *App) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

// jwt signs the App's short-lived authentication token.
func (a *App) jwt() (string, error) {
	now := a.now()
	var iss any = a.AppID
	if n, err := strconv.ParseInt(a.AppID, 10, 64); err == nil {
		iss = n
	}
	header, _ := json.Marshal(map[string]string{"alg": "RS256", "typ": "JWT"})
	claims, _ := json.Marshal(map[string]any{
		// Backdated for clock drift; GitHub caps the lifetime at ten minutes.
		"iat": now.Add(-60 * time.Second).Unix(), "exp": now.Add(9 * time.Minute).Unix(), "iss": iss,
	})
	input := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(claims)
	digest := sha256.Sum256([]byte(input))
	sig, err := rsa.SignPKCS1v15(rand.Reader, a.Key, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	return input + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

// Mint creates an installation token for exactly one repository (owner/name)
// with exactly the given permissions, and checks GitHub granted no more.
func (a *App) Mint(ctx context.Context, repository string, permissions map[string]string) (Token, error) {
	owner, name, ok := strings.Cut(repository, "/")
	if !ok || owner == "" || name == "" {
		return Token{}, fmt.Errorf("repository %q is not owner/name", repository)
	}
	if len(permissions) == 0 {
		return Token{}, errors.New("a downscoped token needs explicit permissions")
	}
	appJWT, err := a.jwt()
	if err != nil {
		return Token{}, err
	}
	body, _ := json.Marshal(map[string]any{"repositories": []string{name}, "permissions": permissions})
	url := strings.TrimRight(a.APIURL, "/") + "/app/installations/" + a.InstallationID + "/access_tokens"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return Token{}, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "Bearer "+appJWT)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client().Do(req)
	if err != nil {
		return Token{}, err
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return Token{}, err
	}
	if resp.StatusCode != http.StatusCreated {
		var msg struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(respBody, &msg)
		return Token{}, fmt.Errorf("GitHub installation token for %s: HTTP %d: %s", repository, resp.StatusCode, msg.Message)
	}
	var out struct {
		Token        string            `json:"token"`
		ExpiresAt    time.Time         `json:"expires_at"`
		Permissions  map[string]string `json:"permissions"`
		Repositories []struct {
			FullName string `json:"full_name"`
		} `json:"repositories"`
	}
	if err := json.Unmarshal(respBody, &out); err != nil {
		return Token{}, fmt.Errorf("decode installation token: %w", err)
	}
	if out.Token == "" || out.ExpiresAt.IsZero() {
		return Token{}, errors.New("installation token response has no token or expiry")
	}
	if err := checkScope(repository, permissions, out.Permissions, len(out.Repositories) == 1 && strings.EqualFold(out.Repositories[0].FullName, repository)); err != nil {
		// Never keep a token that is not the one asked for.
		if revokeErr := a.revoke(ctx, out.Token); revokeErr != nil {
			return Token{}, fmt.Errorf("%w; revoking it also failed: %v", err, revokeErr)
		}
		return Token{}, err
	}
	return Token{Token: out.Token, ExpiresAt: out.ExpiresAt, Repository: repository, Permissions: out.Permissions}, nil
}

// checkScope fails closed: the token must name exactly the one repository
// and grant every requested permission at exactly the requested level, plus
// at most the metadata:read GitHub adds to every installation token.
func checkScope(repository string, requested, granted map[string]string, oneRepository bool) error {
	if !oneRepository {
		return fmt.Errorf("installation token for %s is not scoped to exactly that repository", repository)
	}
	if len(granted) == 0 {
		return fmt.Errorf("installation token for %s reports no permissions", repository)
	}
	for perm, level := range requested {
		if granted[perm] != level {
			return fmt.Errorf("installation token for %s grants %s:%q, requested %q", repository, perm, granted[perm], level)
		}
	}
	for perm, level := range granted {
		if _, ok := requested[perm]; ok {
			continue
		}
		if perm != "metadata" || level != "read" {
			return fmt.Errorf("installation token for %s grants unrequested %s:%s", repository, perm, level)
		}
	}
	return nil
}

// revoke deletes an installation token (DELETE /installation/token).
func (a *App) revoke(ctx context.Context, token string) error {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, strings.TrimRight(a.APIURL, "/")+"/installation/token", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	resp, err := a.client().Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("revoke returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func (a *App) client() *http.Client {
	if a.Client != nil {
		return a.Client
	}
	return &http.Client{Timeout: 30 * time.Second}
}
