package codexchatgpt

import (
	"net/http"
	"testing"
	"time"
)

func TestParseQuotaHeaders(t *testing.T) {
	now := time.Unix(1_800_000_000, 0).UTC()
	h := http.Header{}
	h.Set("x-codex-primary-used-percent", "42.5")
	h.Set("x-codex-primary-window-minutes", "300")
	h.Set("x-codex-primary-reset-at", "1800003600")
	h.Set("x-codex-secondary-used-percent", "0")
	obs, ok := ParseQuotaHeaders(h, http.StatusOK, now)
	if !ok || obs.Status != "allowed" || len(obs.Windows) != 1 {
		t.Fatalf("ParseQuotaHeaders() = %+v, %v", obs, ok)
	}
	w := obs.Windows[0]
	if w.Name != "primary" || w.UsedPercent != 42.5 || w.WindowMinutes != 300 || w.ResetsAt != "2027-01-15T09:00:00Z" {
		t.Fatalf("primary window = %+v", w)
	}

	if _, ok := ParseQuotaHeaders(http.Header{"X-Codex-Turn-State": {"x"}}, http.StatusOK, now); ok {
		t.Fatal("routing headers alone produced an observation")
	}

	rejected := http.Header{}
	rejected.Set("x-codex-rate-limit-reached-type", "primary")
	obs, ok = ParseQuotaHeaders(rejected, http.StatusOK, now)
	if !ok || obs.Status != "rejected" || obs.ReachedType != "primary" {
		t.Fatalf("reached-type observation = %+v, %v", obs, ok)
	}
	obs, ok = ParseQuotaHeaders(http.Header{}, http.StatusTooManyRequests, now)
	if !ok || obs.Status != "rejected" {
		t.Fatalf("429 observation = %+v, %v", obs, ok)
	}
	bad := http.Header{}
	bad.Set("x-codex-primary-used-percent", "NaN")
	if _, ok := ParseQuotaHeaders(bad, http.StatusOK, now); ok {
		t.Fatal("NaN used percent produced an observation")
	}
}
