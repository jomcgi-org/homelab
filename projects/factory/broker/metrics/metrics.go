// Package metrics holds the factory broker's Prometheus metrics. Every label
// takes values from a fixed set or from configuration, never from requests.
package metrics

import (
	shared "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/metrics"
	"github.com/prometheus/client_golang/prometheus"
)

// Prefix names the broker's metric families, including the shared token
// broker refresh and quota families.
const Prefix = "factory_broker"

// GrantStates are the values of the grant state gauge's state label.
var GrantStates = []string{"ready", "login_required", "missing", "error"}

type Metrics struct {
	// Shared refresh counters from the token broker library.
	Refresh *shared.Metrics
	// GrantState is 1 for the grant's current state and 0 otherwise.
	GrantState *prometheus.GaugeVec
	// GrantExpiry is the access token expiry as Unix seconds.
	GrantExpiry *prometheus.GaugeVec
	// RefreshOutcomes counts the periodic refresh check by outcome.
	RefreshOutcomes *prometheus.CounterVec
	// RelayRequests counts relay responses by outcome and HTTP status class.
	RelayRequests *prometheus.CounterVec
	// ActorRejections counts actor JWT rejections by reason.
	ActorRejections *prometheus.CounterVec
	// GitHubMints counts installation token mints by kind, binding and
	// outcome.
	GitHubMints *prometheus.CounterVec
}

func New() *Metrics {
	return &Metrics{
		Refresh: shared.NewWithPrefix(Prefix),
		GrantState: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: Prefix + "_codex_grant_state", Help: "Codex grant state: 1 for the current state.",
		}, []string{"grant", "state"}),
		GrantExpiry: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: Prefix + "_codex_grant_expires_at_seconds", Help: "Codex access token expiry as Unix seconds.",
		}, []string{"grant"}),
		RefreshOutcomes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: Prefix + "_codex_refresh_checks_total", Help: "Periodic Codex grant refresh checks by outcome.",
		}, []string{"grant", "outcome"}),
		RelayRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: Prefix + "_relay_requests_total", Help: "Codex relay requests by outcome and response status class.",
		}, []string{"outcome", "code"}),
		ActorRejections: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: Prefix + "_actor_jwt_rejections_total", Help: "Rejected actor JWTs by reason.",
		}, []string{"reason"}),
		GitHubMints: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: Prefix + "_github_mints_total", Help: "GitHub installation token mints by kind, binding and outcome.",
		}, []string{"kind", "binding", "outcome"}),
	}
}

func (m *Metrics) Register(r prometheus.Registerer) {
	m.Refresh.Register(r)
	r.MustRegister(m.GrantState, m.GrantExpiry, m.RefreshOutcomes, m.RelayRequests, m.ActorRejections, m.GitHubMints)
}

// SetGrantState marks state as the grant's only current state.
func (m *Metrics) SetGrantState(grant, state string) {
	for _, s := range GrantStates {
		v := 0.0
		if s == state {
			v = 1
		}
		m.GrantState.WithLabelValues(grant, s).Set(v)
	}
}

// StatusClass buckets an HTTP status as "2xx", "4xx" and so on.
func StatusClass(code int) string {
	if code < 100 || code > 599 {
		return "other"
	}
	return string(rune('0'+code/100)) + "xx"
}
