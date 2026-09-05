package metrics

import (
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	"github.com/prometheus/client_golang/prometheus"
)

type Metrics struct {
	Refreshes       prometheus.Counter
	RefreshFailures prometheus.Counter
	ReusedRetries   prometheus.Counter
}

func New() *Metrics {
	return &Metrics{
		Refreshes:       prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_total", Help: "Token refresh attempts."}),
		RefreshFailures: prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_failures_total", Help: "Failed token refreshes."}),
		ReusedRetries:   prometheus.NewCounter(prometheus.CounterOpts{Name: "tokenbroker_refresh_token_reused_total", Help: "Refresh token reuse retries."}),
	}
}

func (m *Metrics) Register(r prometheus.Registerer) {
	r.MustRegister(m.Refreshes, m.RefreshFailures, m.ReusedRetries)
}

type quotaCollector struct {
	store     *quota.Store
	providers []string
	used      *prometheus.Desc
	resetsAt  *prometheus.Desc
	exhausted *prometheus.Desc
	observed  *prometheus.Desc
}

func NewQuotaCollector(store *quota.Store, providers []string) prometheus.Collector {
	return &quotaCollector{
		store:     store,
		providers: append([]string(nil), providers...),
		used: prometheus.NewDesc(
			"tokenbroker_quota_used_percent",
			"Latest observed quota utilization percentage.",
			[]string{"provider", "window"}, nil,
		),
		resetsAt: prometheus.NewDesc(
			"tokenbroker_quota_resets_at_seconds",
			"Latest observed quota reset time as Unix seconds.",
			[]string{"provider", "window"}, nil,
		),
		exhausted: prometheus.NewDesc(
			"tokenbroker_quota_exhausted",
			"Whether the latest provider quota observation is exhausted.",
			[]string{"provider"}, nil,
		),
		observed: prometheus.NewDesc(
			"tokenbroker_quota_observed_at_seconds",
			"Latest quota observation time as Unix seconds.",
			[]string{"provider"}, nil,
		),
	}
}

func (c *quotaCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.used
	ch <- c.resetsAt
	ch <- c.exhausted
	ch <- c.observed
}

func (c *quotaCollector) Collect(ch chan<- prometheus.Metric) {
	for _, provider := range c.providers {
		view := c.store.Get(provider)
		if !view.Observed {
			continue
		}
		exhausted := 0.0
		if view.Exhausted {
			exhausted = 1
		}
		ch <- prometheus.MustNewConstMetric(c.exhausted, prometheus.GaugeValue, exhausted, provider)
		if observedAt, err := time.Parse(time.RFC3339, view.ObservedAt); err == nil {
			ch <- prometheus.MustNewConstMetric(c.observed, prometheus.GaugeValue, float64(observedAt.Unix()), provider)
		}
		for _, window := range view.Windows {
			ch <- prometheus.MustNewConstMetric(c.used, prometheus.GaugeValue, window.UsedPercent, provider, window.Name)
			if window.ResetsAt == "" {
				continue
			}
			reset, err := time.Parse(time.RFC3339, window.ResetsAt)
			if err != nil {
				continue
			}
			ch <- prometheus.MustNewConstMetric(c.resetsAt, prometheus.GaugeValue, float64(reset.Unix()), provider, window.Name)
		}
	}
}
