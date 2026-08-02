package codexchatgpt

import (
	"context"
	"encoding/json"
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
	return c.tokenRequest(ctx, url.Values{"grant_type": {"authorization_code"}, "code": {code.AuthorizationCode}, "client_id": {ClientID}, "redirect_uri": {"http://localhost:1455/auth/callback"}, "code_verifier": {code.CodeVerifier}, "code_challenge_method": {"S256"}})
}

func (c *Adapter) RefreshToken(ctx context.Context, refreshToken string) (provider.TokenResponse, error) {
	return c.tokenRequest(ctx, url.Values{"grant_type": {"refresh_token"}, "refresh_token": {refreshToken}, "client_id": {ClientID}})
}

func (c *Adapter) tokenRequest(ctx context.Context, values url.Values) (provider.TokenResponse, error) {
	var out provider.TokenResponse
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.issuer()+"/oauth/token", strings.NewReader(values.Encode()))
	if err != nil {
		return out, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return out, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, err
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return out, fmt.Errorf("decode token response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		if out.Error != "" {
			return out, fmt.Errorf("oauth token request: %s", out.Error)
		}
		return out, fmt.Errorf("oauth token request returned %s", resp.Status)
	}
	return out, nil
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
