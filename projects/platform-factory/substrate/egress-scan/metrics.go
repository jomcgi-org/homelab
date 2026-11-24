package main

import (
	"context"
	"errors"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/codes"
)

const capacityReason = "scan capacity exhausted"

type meteredScanner struct {
	*scanner
	checks   *prometheus.CounterVec
	duration *prometheus.HistogramVec
}

func newMeteredScanner(scanner *scanner, registry prometheus.Registerer) *meteredScanner {
	buckets := []float64{.001, .0025, .005, .01, .025, .05, .075, .08, .1, .25}
	outcomes := []string{"allowed", "credential_blocked", "invalid_request", "capacity_limited", "work_limit", "cancelled", "error"}
	scanner.metrics = newDetectorMetrics(registry)
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
		if status.GetMessage() == capacityReason {
			return "capacity_limited"
		}
		return "work_limit"
	default:
		return "error"
	}
}

// detectorMetrics counts what the detector found and why a call had no verdict.
// The series appear with the first finding or failure, so an idle scanner adds
// none. The rule label has one value for each rule of the detector.
type detectorMetrics struct {
	findings *prometheus.CounterVec
	failures *prometheus.CounterVec
}

func newDetectorMetrics(registry prometheus.Registerer) *detectorMetrics {
	m := &detectorMetrics{
		findings: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_detector_findings_total",
			Help: "Detector findings by detector, rule, class and mode. In observe mode a block finding is counted and never denies.",
		}, []string{"detector", "rule", "class", "mode"}),
		failures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_detector_failures_total",
			Help: "Detector calls without a verdict by detector, reason and mode.",
		}, []string{"detector", "reason", "mode"}),
	}
	registry.MustRegister(m.findings, m.failures)
	return m
}

func (m *detectorMetrics) record(host *detect.Host, v detect.Verdict) {
	if m == nil {
		return
	}
	name, mode := host.Name(), string(host.Config().Mode)
	for _, f := range v.Findings {
		m.findings.WithLabelValues(name, f.RuleID, string(f.Class), mode).Inc()
	}
	if v.Failure != detect.FailureNone {
		m.failures.WithLabelValues(name, string(v.Failure), mode).Inc()
	}
}
