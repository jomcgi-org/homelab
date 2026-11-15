package codexchatgpt

import (
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
)

// ParseQuotaHeaders reads the x-codex-* rate-limit headers the ChatGPT
// backend sets on a Codex response. It follows the EmberVM egress proxy's
// parser (projects/firecracker/substrate/egress-proxy/cmd/quota.go): a
// response with no quota windows is an observation only when it is a
// rejection, so routing headers on an allowed turn never replace a real
// reading.
func ParseQuotaHeaders(h http.Header, statusCode int, now time.Time) (quota.Observation, bool) {
	reachedType := strings.TrimSpace(h.Get("x-codex-rate-limit-reached-type"))
	status := "allowed"
	if statusCode == http.StatusTooManyRequests || reachedType != "" {
		status = "rejected"
	}
	obs := quota.Observation{
		Provider:    "codex",
		ObservedAt:  now.UTC().Format(time.RFC3339),
		Status:      status,
		ReachedType: reachedType,
		Windows:     make([]quota.Window, 0, 2),
	}
	for _, name := range []string{"primary", "secondary"} {
		prefix := "x-codex-" + name + "-"
		used, ok := finiteFloat(h.Get(prefix + "used-percent"))
		if !ok {
			continue
		}
		window := quota.Window{Name: name, UsedPercent: used}
		hasMinutes := false
		if minutes, err := strconv.Atoi(strings.TrimSpace(h.Get(prefix + "window-minutes"))); err == nil && minutes > 0 {
			window.WindowMinutes = minutes
			hasMinutes = true
		}
		hasReset := false
		if seconds, err := strconv.ParseInt(strings.TrimSpace(h.Get(prefix+"reset-at")), 10, 64); err == nil && seconds > 0 {
			window.ResetsAt = time.Unix(seconds, 0).UTC().Format(time.RFC3339)
			hasReset = true
		}
		// Zero metadata does not establish a real window for an unused slot.
		if used == 0 && !hasMinutes && !hasReset {
			continue
		}
		obs.Windows = append(obs.Windows, window)
	}
	if len(obs.Windows) == 0 && status != "rejected" {
		return quota.Observation{}, false
	}
	return obs, true
}

func finiteFloat(raw string) (float64, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, false
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsNaN(value) || math.IsInf(value, 0) {
		return 0, false
	}
	return value, true
}
