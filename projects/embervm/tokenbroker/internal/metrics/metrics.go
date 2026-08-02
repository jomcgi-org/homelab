package metrics

import "github.com/prometheus/client_golang/prometheus"

type Metrics struct {
	Refreshes       prometheus.Counter
	RefreshFailures prometheus.Counter
	ReusedRetries   prometheus.Counter
}

func New() *Metrics {
	return &Metrics{Refreshes: prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_total", Help: "Token refresh attempts."}), RefreshFailures: prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_failures_total", Help: "Failed token refreshes."}), ReusedRetries: prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_token_reused_total", Help: "Refresh token reuse retries."})}
}
