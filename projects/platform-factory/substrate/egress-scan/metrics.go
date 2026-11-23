package main

import (
	"context"
	"errors"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/codes"
)

const (
	capacityReason = "scan capacity exhausted"
	// engineUnavailableOutcome counts requests that the second tier denied
	// because the engine gave no verdict. It exists only when the tier is on.
	engineUnavailableOutcome = "engine_unavailable"
)

type meteredScanner struct {
	*scanner
	checks   *prometheus.CounterVec
	duration *prometheus.HistogramVec
}

func newMeteredScanner(scanner *scanner, registry prometheus.Registerer) *meteredScanner {
	buckets := []float64{.001, .0025, .005, .01, .025, .05, .075, .08, .1, .25}
	outcomes := []string{"allowed", "credential_blocked", "invalid_request", "capacity_limited", "work_limit", "cancelled", "error"}
	if scanner.semgrep != nil {
		// The tier can hold a check for its full deadline.
		buckets = append(buckets, .3, .4, .5, 1)
		outcomes = append(outcomes, engineUnavailableOutcome)
		scanner.semgrep.metrics.register(registry)
	}
	checks := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "factory_egress_scan_checks_total",
		Help: "Completed egress authorization checks by verdict.",
	}, []string{"outcome"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "factory_egress_scan_check_duration_seconds",
		Help:    "Time in the egress authorization handler, including request decoding.",
		Buckets: buckets,
	}, []string{"outcome"})
	for _, outcome := range outcomes {
		checks.WithLabelValues(outcome)
		duration.WithLabelValues(outcome)
	}
	registry.MustRegister(checks, duration)
	return &meteredScanner{scanner: scanner, checks: checks, duration: duration}
}

func (s *meteredScanner) Check(ctx context.Context, req *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	start := time.Now()
	response, err := s.scanner.Check(ctx, req)
	elapsed := time.Since(start).Seconds()
	outcome := checkOutcome(response, err)
	s.checks.WithLabelValues(outcome).Inc()
	s.duration.WithLabelValues(outcome).Observe(elapsed)
	return response, err
}

func checkOutcome(response *authv3.CheckResponse, err error) string {
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return "cancelled"
	}
	status := response.GetStatus()
	if err != nil || status == nil {
		return "error"
	}
	switch codes.Code(status.GetCode()) {
	case codes.OK:
		return "allowed"
	case codes.PermissionDenied:
		return "credential_blocked"
	case codes.InvalidArgument:
		return "invalid_request"
	case codes.ResourceExhausted:
		switch status.GetMessage() {
		case capacityReason:
			return "capacity_limited"
		case engineUnavailableReason:
			return engineUnavailableOutcome
		}
		return "work_limit"
	default:
		return "error"
	}
}

// tierMetrics are the second tier series. They exist only when the tier is on,
// so the /metrics output of a scanner with the tier off does not change.
type tierMetrics struct {
	requests    *prometheus.CounterVec
	duration    *prometheus.HistogramVec
	inflight    prometheus.Gauge
	failureMode *prometheus.CounterVec
	agreement   *prometheus.CounterVec
	breakerOpen prometheus.GaugeFunc
	deadline    prometheus.Gauge
}

func newTierMetrics(t *semgrepTier) *tierMetrics {
	m := &tierMetrics{
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_semgrep_requests_total",
			Help: "Engine calls by outcome. Timeout, error, shed, breaker_open and oversize apply the failure mode.",
		}, []string{"outcome"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "factory_egress_scan_semgrep_duration_seconds",
			Help:    "Time in the engine tier per call, by outcome.",
			Buckets: []float64{.005, .01, .025, .05, .075, .1, .15, .2, .25, .3, .4, .5, 1, 2.5, 5, 10},
		}, []string{"outcome"}),
		inflight: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_egress_scan_semgrep_inflight",
			Help: "Engine calls in progress.",
		}),
		failureMode: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_semgrep_failure_mode_total",
			Help: "Requests where the engine gave no verdict and the failure mode decided, by action. The allow action lets the request through.",
		}, []string{"action"}),
		agreement: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_semgrep_agreement_total",
			Help: "Requests where both tiers gave a verdict, by Gitleaks and engine result.",
		}, []string{"gitleaks", "semgrep"}),
		breakerOpen: prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: "factory_egress_scan_semgrep_breaker_open",
			Help: "1 while the breaker refuses engine calls.",
		}, func() float64 {
			if t.breaker.open() {
				return 1
			}
			return 0
		}),
		deadline: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_egress_scan_semgrep_deadline_seconds",
			Help: "Configured engine deadline. Keep the gateway requestTimeout 10 ms above it.",
		}),
	}
	m.deadline.Set(t.cfg.Deadline.Seconds())
	for _, outcome := range engineOutcomes {
		m.requests.WithLabelValues(outcome)
		m.duration.WithLabelValues(outcome)
	}
	for _, action := range []string{failureDeny, failureAllow} {
		m.failureMode.WithLabelValues(action)
	}
	for _, g := range []string{"found", "clean"} {
		for _, s := range []string{"found", "clean"} {
			m.agreement.WithLabelValues(g, s)
		}
	}
	return m
}

func (m *tierMetrics) register(registry prometheus.Registerer) {
	registry.MustRegister(m.requests, m.duration, m.inflight, m.failureMode, m.agreement, m.breakerOpen, m.deadline)
}

func (m *tierMetrics) observe(outcome string, elapsed time.Duration) {
	m.requests.WithLabelValues(outcome).Inc()
	m.duration.WithLabelValues(outcome).Observe(elapsed.Seconds())
}

// agree counts a request when both tiers gave a verdict.
func (m *tierMetrics) agree(gitleaksFound bool, res engineResult) {
	if res.outcome != engineFound && res.outcome != engineClean {
		return
	}
	g := "clean"
	if gitleaksFound {
		g = "found"
	}
	m.agreement.WithLabelValues(g, res.outcome).Inc()
}
