package codexchatgpt

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
)

const ClientID = "app_EMoamEEZ73f0CkXaXp7hrann"

type Adapter struct {
	HTTPClient *http.Client
	Issuer     string
}

func (c *Adapter) httpClient() *http.Client {
	if c.HTTPClient != nil {
		return c.HTTPClient
	}
	return http.DefaultClient
}

func (c *Adapter) issuer() string {
	if c.Issuer != "" {
		return strings.TrimRight(c.Issuer, "/")
	}
	return "https://auth.openai.com"
}

func (c *Adapter) StartDeviceFlow(ctx context.Context) (provider.DeviceCodeResponse, error) {
	var out provider.DeviceCodeResponse
	if err := c.postJSON(ctx, "/api/accounts/deviceauth/usercode", map[string]string{"client_id": ClientID}, &out); err != nil {
		return out, err
	}
	if out.VerificationURL == "" {
		out.VerificationURL = c.issuer() + "/codex/device"
	}
	if out.Interval <= 0 {
		out.Interval = 5
	}
	return out, nil
}

func (c *Adapter) PollForAuthorization(ctx context.Context, code provider.DeviceCodeResponse) (provider.AuthorizationCodeResponse, error) {
	var out provider.AuthorizationCodeResponse
	deadline := time.Now().Add(15 * time.Minute)
	interval := time.Duration(code.Interval) * time.Second
	if interval <= 0 {
		interval = 5 * time.Second
	}
	for {
		if time.Now().After(deadline) {
			return out, fmt.Errorf("device authorization timed out")
		}
		resp, err := c.post(ctx, "/api/accounts/deviceauth/token", map[string]string{"device_auth_id": code.DeviceAuthID, "user_code": code.UserCode})
		if err != nil {
			return out, err
		}
		body, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			return out, readErr
		}
		if resp.StatusCode == http.StatusOK {
			if err := json.Unmarshal(body, &out); err != nil {
				return out, err
			}
			return out, nil
		}
		if resp.StatusCode != http.StatusForbidden && resp.StatusCode != http.StatusNotFound {
			return out, fmt.Errorf("device token polling returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
		}
		t := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			t.Stop()
			return out, ctx.Err()
		case <-t.C:
		}
	}
}

func (c *Adapter) ExchangeCode(ctx context.Context, code provider.AuthorizationCodeResponse) (provider.TokenResponse, error) {
	// Keep this order and field set aligned with Codex login/src/server.rs:
	// grant_type, code, redirect_uri, client_id, code_verifier. Device auth does
	// not send code_challenge_method on the token exchange.
	body := "grant_type=authorization_code&code=" + url.QueryEscape(code.AuthorizationCode) +
		"&redirect_uri=" + url.QueryEscape(c.issuer()+"/deviceauth/callback") +
		"&client_id=" + url.QueryEscape(ClientID) +
		"&code_verifier=" + url.QueryEscape(code.CodeVerifier)
	return c.tokenRequestBody(ctx, body)
}

func (c *Adapter) RefreshToken(ctx context.Context, refreshToken string) (provider.TokenResponse, error) {
	return c.tokenRequest(ctx, url.Values{"grant_type": {"refresh_token"}, "refresh_token": {refreshToken}, "client_id": {ClientID}})
}

func (c *Adapter) tokenRequest(ctx context.Context, values url.Values) (provider.TokenResponse, error) {
	return c.tokenRequestBody(ctx, values.Encode())
}

func (c *Adapter) tokenRequestBody(ctx context.Context, body string) (provider.TokenResponse, error) {
	var out provider.TokenResponse
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.issuer()+"/oauth/token", strings.NewReader(body))
	if err != nil {
		return out, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return out, err
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, err
	}
	if err := json.Unmarshal(respBody, &out); err != nil {
		return out, fmt.Errorf("decode token response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		if out.Error != "" {
			if out.Error == "refresh_token_reused" {
				return out, fmt.Errorf("oauth token request: %w", provider.ErrRefreshTokenReused)
			}
			if out.Error == "invalid_grant" || out.Error == "invalid_request" || out.Error == "expired_token" {
				return out, fmt.Errorf("oauth token request: %s: %w", out.Error, provider.ErrInvalidGrant)
			}
			return out, fmt.Errorf("oauth token request: %s", out.Error)
		}
		return out, fmt.Errorf("oauth token request returned %s", resp.Status)
	}
	if out.ExpiresIn > 0 {
		out.ExpiresAt = time.Now().Add(time.Duration(out.ExpiresIn) * time.Second)
	} else if out.AccessToken != "" {
		out.ExpiresAt, err = jwtExpiry(out.AccessToken)
		if err != nil {
			return out, fmt.Errorf("parse access token expiry: %w", err)
		}
	}
	return out, nil
}

func jwtExpiry(token string) (time.Time, error) {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return time.Time{}, errors.New("access token is not a JWT")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return time.Time{}, err
	}
	var claims struct {
		Exp int64 `json:"exp"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil {
		return time.Time{}, err
	}
	if claims.Exp == 0 {
		return time.Time{}, errors.New("access token has no exp")
	}
	return time.Unix(claims.Exp, 0), nil
}

func (c *Adapter) postJSON(ctx context.Context, path string, payload any, out any) error {
	resp, err := c.post(ctx, path, payload)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("oauth request returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	return json.Unmarshal(body, out)
}

func (c *Adapter) post(ctx context.Context, path string, payload any) (*http.Response, error) {
	b, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.issuer()+path, strings.NewReader(string(b)))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return c.httpClient().Do(req)
}
