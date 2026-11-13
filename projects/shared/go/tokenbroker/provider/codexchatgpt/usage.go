package codexchatgpt

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
)

// AccountID returns the ChatGPT account ID claim from a Codex access token.
func AccountID(token string) (string, bool) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", false
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", false
	}
	var claims struct {
		Auth struct {
			AccountID string `json:"chatgpt_account_id"`
		} `json:"https://api.openai.com/auth"`
	}
	if json.Unmarshal(payload, &claims) != nil || claims.Auth.AccountID == "" {
		return "", false
	}
	return claims.Auth.AccountID, true
}

type usageWindow struct {
	UsedPercent       *int   `json:"used_percent"`
	WindowSeconds     *int   `json:"limit_window_seconds"`
	ResetAt           *int64 `json:"reset_at"`
	ResetAfterSeconds *int   `json:"reset_after_seconds"`
}

// DecodeUsage parses a ChatGPT backend /wham/usage response into a quota
// observation for the "codex" provider.
func DecodeUsage(body io.Reader, now time.Time) (quota.Observation, error) {
	var payload struct {
		RateLimit *struct {
			Allowed      *bool        `json:"allowed"`
			LimitReached *bool        `json:"limit_reached"`
			Primary      *usageWindow `json:"primary_window"`
			Secondary    *usageWindow `json:"secondary_window"`
		} `json:"rate_limit"`
	}
	decoder := json.NewDecoder(io.LimitReader(body, 64<<10))
	if err := decoder.Decode(&payload); err != nil {
		return quota.Observation{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return quota.Observation{}, errors.New("trailing usage data")
	}
	limit := payload.RateLimit
	if limit == nil || limit.Allowed == nil || limit.LimitReached == nil {
		return quota.Observation{}, errors.New("missing rate limit details")
	}
	obs := quota.Observation{Provider: "codex", ObservedAt: now.UTC().Format(time.RFC3339), Status: "allowed"}
	if *limit.LimitReached || !*limit.Allowed {
		obs.Status = "rejected"
	}
	for _, entry := range []struct {
		name   string
		window *usageWindow
	}{{"primary", limit.Primary}, {"secondary", limit.Secondary}} {
		window := entry.window
		if window == nil {
			continue
		}
		if window.UsedPercent == nil || window.WindowSeconds == nil || window.ResetAt == nil || window.ResetAfterSeconds == nil {
			return quota.Observation{}, fmt.Errorf("incomplete %s usage window", entry.name)
		}
		obs.Windows = append(obs.Windows, quota.Window{
			Name: entry.name, UsedPercent: float64(*window.UsedPercent), WindowMinutes: *window.WindowSeconds / 60,
			ResetsAt: time.Unix(*window.ResetAt, 0).UTC().Format(time.RFC3339),
		})
	}
	if obs.Status == "allowed" && len(obs.Windows) == 0 {
		return quota.Observation{}, errors.New("allowed usage has no windows")
	}
	if !quota.ValidObservation(obs, now.UTC()) {
		return quota.Observation{}, errors.New("invalid usage observation")
	}
	return obs, nil
}
