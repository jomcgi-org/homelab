// Package meter counts every call by tool, surface and client class. It is
// the usage record the free tier is measured by and that a paid tier would
// bill from, so it counts at the origin for every request that reaches it.
// Edge cache hits never reach the origin; the hit ratio comes from
// Cloudflare's analytics, and here everything is by definition a miss. Export
// to Honeycomb and the unit price table are #7245.
package meter

import (
	"net/http"
	"net/netip"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// Client classes.
const (
	ClassAnthropic = "anthropic"
	ClassOpenAI    = "openai"
	ClassDirect    = "direct"
	ClassUnknown   = "unknown"
)

// anthropicRanges are Anthropic's published egress ranges for outbound MCP
// calls (platform.claude.com/docs/en/api/ip-addresses). Re-check the page
// when changing them; the Envoy assistant bucket (route.assistantBucket in
// the chart) must match, and its acceptance test checks that it does.
var anthropicRanges = []netip.Prefix{
	netip.MustParsePrefix("160.79.104.0/23"),
	netip.MustParsePrefix("2607:6bc0::/48"),
}

// ClientClass classifies a request by the client address Cloudflare reports
// and, for assistants without published ranges, by User-Agent. The class is a
// metering label, never an authorization decision: CF-Connecting-IP is only
// trustworthy because the sole ingress path is cloudflared.
func ClientClass(r *http.Request) string {
	raw := strings.TrimSpace(r.Header.Get("CF-Connecting-IP"))
	if raw == "" {
		// In-cluster or local traffic that never crossed the edge.
		return ClassDirect
	}
	ip, err := netip.ParseAddr(raw)
	if err != nil {
		return ClassUnknown
	}
	ip = ip.Unmap()
	for _, p := range anthropicRanges {
		if p.Contains(ip) {
			return ClassAnthropic
		}
	}
	ua := strings.ToLower(r.Header.Get("User-Agent"))
	if strings.Contains(ua, "openai") || strings.Contains(ua, "chatgpt") {
		return ClassOpenAI
	}
	return ClassDirect
}

// Meter holds the counters.
type Meter struct {
	calls   *prometheus.CounterVec
	results *prometheus.CounterVec
	latency *prometheus.HistogramVec
}

// New registers the meter's collectors on reg.
func New(reg prometheus.Registerer) *Meter {
	m := &Meter{
		calls: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "scotland_connector_calls_total",
			Help: "Calls by tool, surface (api, mcp), client class and outcome.",
		}, []string{"tool", "surface", "client_class", "outcome"}),
		results: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "scotland_connector_results_total",
			Help: "Items returned by tool, surface and client class.",
		}, []string{"tool", "surface", "client_class"}),
		latency: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "scotland_connector_call_duration_seconds",
			Help:    "In-process handling time by tool and surface.",
			Buckets: []float64{0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05},
		}, []string{"tool", "surface"}),
	}
	reg.MustRegister(m.calls, m.results, m.latency)
	return m
}

// Record counts one call.
func (m *Meter) Record(tool, surface, class, outcome string, results int, d time.Duration) {
	m.calls.WithLabelValues(tool, surface, class, outcome).Inc()
	if results > 0 {
		m.results.WithLabelValues(tool, surface, class).Add(float64(results))
	}
	m.latency.WithLabelValues(tool, surface).Observe(d.Seconds())
}
