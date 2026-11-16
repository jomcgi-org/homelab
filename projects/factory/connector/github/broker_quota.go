package github

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// DefaultQuotaPath is the broker endpoint that reports the Codex grant's
// quota to the connector.
const DefaultQuotaPath = "/codex/quota"

// QuotaReading is the broker's latest quota reading for its Codex grant.
type QuotaReading struct {
	// Exhausted is true while the reading refuses calls until ResetsAt.
	Exhausted bool
	ResetsAt  time.Time
	// ReceivedAt is when the broker received the reading, zero before its
	// first one.
	ReceivedAt time.Time
}

// BrokerQuota reads the broker's quota reading, so the connector can resume
// work a quota failure paused (HOME-127).
type BrokerQuota struct {
	// URL is the broker's base URL.
	URL    string
	Bearer Bearer
	HTTP   *http.Client
}

// Quota returns the broker's latest reading.
func (b *BrokerQuota) Quota(ctx context.Context) (QuotaReading, error) {
	bearer, err := b.Bearer.Token()
	if err != nil {
		return QuotaReading{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(b.URL, "/")+DefaultQuotaPath, nil)
	if err != nil {
		return QuotaReading{}, err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	hc := b.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return QuotaReading{}, fmt.Errorf("broker: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return QuotaReading{}, err
	}
	if resp.StatusCode != http.StatusOK {
		return QuotaReading{}, fmt.Errorf("broker: %s: HTTP %d: %.200s", DefaultQuotaPath, resp.StatusCode, data)
	}
	var out struct {
		Exhausted  bool   `json:"exhausted"`
		ResetsAt   string `json:"resets_at"`
		ReceivedAt string `json:"received_at"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return QuotaReading{}, fmt.Errorf("broker: decode %s: %w", DefaultQuotaPath, err)
	}
	r := QuotaReading{Exhausted: out.Exhausted}
	for _, f := range []struct {
		name, text string
		at         *time.Time
	}{{"resets_at", out.ResetsAt, &r.ResetsAt}, {"received_at", out.ReceivedAt, &r.ReceivedAt}} {
		if f.text == "" {
			continue
		}
		if *f.at, err = time.Parse(time.RFC3339Nano, f.text); err != nil {
			return QuotaReading{}, fmt.Errorf("broker: %s %s: %w", DefaultQuotaPath, f.name, err)
		}
	}
	return r, nil
}
