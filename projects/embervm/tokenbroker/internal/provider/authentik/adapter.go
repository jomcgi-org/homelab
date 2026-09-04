package authentik

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

// Adapter mints authentik access tokens with the OAuth2 client_credentials
// grant. authentik's machine-to-machine flow authenticates a SERVICE ACCOUNT,
// so it takes a username and an authentik app password rather than an OAuth
// client secret: there is no client_secret parameter to supply. The field
// names say so on purpose, so a reader does not go hunting for one.
type Adapter struct {
	HTTPClient    *http.Client
	TokenEndpoint string
	ClientID      string
	Scope         string
	Username      string
	AppPassword   string
}

var _ provider.Minter = (*Adapter)(nil)

func (a *Adapter) httpClient() *http.Client {
	if a.HTTPClient != nil {
		return a.HTTPClient
	}
	return http.DefaultClient
}

func (a *Adapter) Mint(ctx context.Context) (provider.TokenResponse, error) {
	var out provider.TokenResponse
	values := url.Values{
		"grant_type": {"client_credentials"},
		"client_id":  {a.ClientID},
		"username":   {a.Username},
		"password":   {a.AppPassword},
		"scope":      {a.Scope},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.TokenEndpoint, strings.NewReader(values.Encode()))
	if err != nil {
		return out, fmt.Errorf("create authentik token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := a.httpClient().Do(req)
	if err != nil {
		return out, fmt.Errorf("send authentik token request: %w", err)
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, fmt.Errorf("read authentik token response: %w", err)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return out, fmt.Errorf("oauth token request returned %d %s", resp.StatusCode, http.StatusText(resp.StatusCode))
	}
	if err := json.Unmarshal(respBody, &out); err != nil {
		return out, fmt.Errorf("decode token response: %w", err)
	}
	if out.AccessToken == "" {
		return out, fmt.Errorf("decode token response: access_token is missing")
	}
	if out.ExpiresIn <= 0 {
		return out, fmt.Errorf("decode token response: expires_in must be positive")
	}
	out.ExpiresAt = time.Now().Add(time.Duration(out.ExpiresIn) * time.Second)
	return out, nil
}
