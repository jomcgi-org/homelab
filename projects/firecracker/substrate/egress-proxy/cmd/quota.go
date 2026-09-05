package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Window is one provider quota window in the shared sidecar/broker contract.
type Window struct {
	Name          string  `json:"name"`
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes,omitempty"`
	ResetsAt      string  `json:"resets_at,omitempty"`
}

// QuotaObservation is the shared JSON contract sent to the token broker.
type QuotaObservation struct {
	Provider    string   `json:"provider"`
	ObservedAt  string   `json:"observed_at"`
	Status      string   `json:"status"`
	ReachedType string   `json:"reached_type"`
	Windows     []Window `json:"windows"`
}

func parseCodexQuota(h http.Header, statusCode int, now time.Time) (QuotaObservation, bool) {
	reachedType := strings.TrimSpace(h.Get("x-codex-rate-limit-reached-type"))
	status := "allowed"
	if statusCode == http.StatusTooManyRequests || reachedType != "" {
		status = "rejected"
	}

	obs := QuotaObservation{
		Provider:    "codex",
		ObservedAt:  now.UTC().Format(time.RFC3339),
		Status:      status,
		ReachedType: reachedType,
		Windows:     make([]Window, 0, 2),
	}
	for _, name := range []string{"primary", "secondary"} {
		prefix := "x-codex-" + name + "-"
		used, ok := parseFiniteFloat(h.Get(prefix + "used-percent"))
		if !ok {
			continue
		}
		window := Window{Name: name, UsedPercent: used}
		if minutes, err := strconv.Atoi(strings.TrimSpace(h.Get(prefix + "window-minutes"))); err == nil && minutes >= 0 {
			window.WindowMinutes = minutes
		}
		if reset, ok := parseEpochReset(h.Get(prefix + "reset-at")); ok {
			window.ResetsAt = reset
		}
		obs.Windows = append(obs.Windows, window)
	}

	// A response with no quota windows is not an observation unless it is a
	// rejection: Codex puts unrelated x-codex-* headers (turn-state, routing
	// hints) on every turn, and an allowed observation with no windows would
	// overwrite a real reading at the broker under latest-wins.
	if len(obs.Windows) == 0 && status != "rejected" {
		return QuotaObservation{}, false
	}
	return obs, true
}

func parseClaudeQuota(h http.Header, statusCode int, now time.Time) (QuotaObservation, bool) {
	status := "unknown"
	switch strings.TrimSpace(h.Get("anthropic-ratelimit-unified-status")) {
	case "allowed":
		status = "allowed"
	case "allowed_warning":
		status = "warning"
	case "rejected":
		status = "rejected"
	}
	if statusCode == http.StatusTooManyRequests {
		status = "rejected"
	}

	obs := QuotaObservation{
		Provider:    "claude",
		ObservedAt:  now.UTC().Format(time.RFC3339),
		Status:      status,
		ReachedType: strings.TrimSpace(h.Get("anthropic-ratelimit-unified-overage-status")),
		Windows:     make([]Window, 0, 2),
	}
	unifiedReset, hasUnifiedReset := parseFlexibleReset(h.Get("anthropic-ratelimit-unified-reset"))
	for _, spec := range []struct {
		name    string
		minutes int
	}{{"5h", 5 * 60}, {"7d", 7 * 24 * 60}} {
		prefix := "anthropic-ratelimit-unified-" + spec.name + "-"
		used, ok := parseFiniteFloat(h.Get(prefix + "utilization"))
		if !ok || used < 0 {
			continue
		}
		if used <= 1 {
			used *= 100
		}
		window := Window{Name: spec.name, UsedPercent: used, WindowMinutes: spec.minutes}
		if reset, ok := parseFlexibleReset(h.Get(prefix + "reset")); ok {
			window.ResetsAt = reset
		} else if hasUnifiedReset {
			window.ResetsAt = unifiedReset
		}
		obs.Windows = append(obs.Windows, window)
	}
	// Same rule as Codex: a status-only reply with no utilization windows must
	// not replace a real reading at the broker unless it is a rejection.
	if len(obs.Windows) == 0 && status != "rejected" {
		return QuotaObservation{}, false
	}
	return obs, true
}

func observeQuota(sec *secretEntry, resp *http.Response, now time.Time) (QuotaObservation, bool) {
	if sec == nil || resp == nil || sec.QuotaProvider == "" {
		return QuotaObservation{}, false
	}
	switch sec.QuotaProvider {
	case "codex":
		return parseCodexQuota(resp.Header, resp.StatusCode, now)
	case "claude":
		return parseClaudeQuota(resp.Header, resp.StatusCode, now)
	default:
		return QuotaObservation{}, false
	}
}

func parseFiniteFloat(raw string) (float64, bool) {
	if strings.TrimSpace(raw) == "" {
		return 0, false
	}
	value, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil || math.IsNaN(value) || math.IsInf(value, 0) {
		return 0, false
	}
	return value, true
}

func parseEpochReset(raw string) (string, bool) {
	seconds, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return "", false
	}
	return time.Unix(seconds, 0).UTC().Format(time.RFC3339), true
}

func parseFlexibleReset(raw string) (string, bool) {
	if reset, ok := parseEpochReset(raw); ok {
		return reset, true
	}
	parsed, err := time.Parse(time.RFC3339, strings.TrimSpace(raw))
	if err != nil {
		return "", false
	}
	return parsed.UTC().Format(time.RFC3339), true
}

func quotaSummary(obs QuotaObservation) (Window, bool) {
	want := "primary"
	if obs.Provider == "claude" {
		want = "5h"
	}
	for _, window := range obs.Windows {
		if window.Name == want {
			return window, true
		}
	}
	return Window{}, false
}

func normalizeBrokerURL(rawURL string) string {
	if rawURL == "" {
		return ""
	}
	if !strings.Contains(rawURL, "://") {
		rawURL = "http://" + rawURL
	}
	return strings.TrimRight(rawURL, "/")
}

// quotaReporter serializes observations to the broker without delaying the
// response relay. At most one POST is active, and only the newest queued
// observation is retained while it is active.
type quotaReporter struct {
	brokerURL string
	client    *http.Client
	logger    *slog.Logger

	pendingMu  sync.Mutex
	pendingObs *QuotaObservation
	inFlight   bool
}

func newQuotaReporter(rawURL string, logger *slog.Logger) *quotaReporter {
	brokerURL := normalizeBrokerURL(rawURL)
	if brokerURL == "" {
		if logger == nil {
			logger = slog.Default()
		}
		logger.Warn("quota reporting is disabled; token broker URL is empty")
		return nil
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &quotaReporter{
		brokerURL: brokerURL,
		client:    &http.Client{Timeout: 5 * time.Second},
		logger:    logger,
	}
}

func (r *quotaReporter) report(obs QuotaObservation) {
	if r == nil || r.brokerURL == "" {
		return
	}
	r.pendingMu.Lock()
	if r.inFlight {
		copy := obs
		r.pendingObs = &copy
		r.pendingMu.Unlock()
		return
	}
	r.inFlight = true
	r.pendingMu.Unlock()
	go r.run(obs)
}

func (r *quotaReporter) run(obs QuotaObservation) {
	for {
		r.post(obs)
		r.pendingMu.Lock()
		if r.pendingObs == nil {
			r.inFlight = false
			r.pendingMu.Unlock()
			return
		}
		obs = *r.pendingObs
		r.pendingObs = nil
		r.pendingMu.Unlock()
	}
}

func (r *quotaReporter) post(obs QuotaObservation) {
	body, err := json.Marshal(obs)
	if err != nil {
		r.logger.Warn("egress quota report failed", "provider", obs.Provider, "err", err)
		return
	}
	endpoint := r.brokerURL + "/quota/" + url.PathEscape(obs.Provider)
	req, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		r.logger.Warn("egress quota report failed", "provider", obs.Provider, "err", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.client.Do(req)
	if err != nil {
		r.logger.Warn("egress quota report failed", "provider", obs.Provider, "err", err)
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		r.logger.Warn("egress quota report failed", "provider", obs.Provider, "err", fmt.Sprintf("broker returned %s", resp.Status))
	}
}
