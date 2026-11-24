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
	if scanner.labels == nil {
		scanner.labels = newLabeler(nil, 0)
	}
	checks := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "factory_egress_scan_checks_total",
		Help: "Completed egress authorization checks by verdict, destination route and source. The route is the destination host and port, bounded: values outside the allowlist are other. The source is the atespace of the actor when the gateway gives it.",
	}, []string{"outcome", "route", "source"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "factory_egress_scan_check_duration_seconds",
		Help:    "Time in the egress authorization handler, including request decoding, by verdict and route.",
		Buckets: buckets,
	}, []string{"outcome", "route"})
	for _, outcome := range outcomes {
		for _, route := range scanner.labels.routeValues() {
			checks.WithLabelValues(outcome, route, labelUnknown)
			duration.WithLabelValues(outcome, route)
		}
	}
	registry.MustRegister(checks, duration)
	return &meteredScanner{scanner: scanner, checks: checks, duration: duration}
}

func (s *meteredScanner) Check(ctx context.Context, req *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	start := time.Now()
	response, err := s.scanner.Check(ctx, req)
	elapsed := time.Since(start).Seconds()
	outcome := checkOutcome(response, err)
	route, source := s.labels.labels(req)
	s.checks.WithLabelValues(outcome, route, source).Inc()
	s.duration.WithLabelValues(outcome, route).Observe(elapsed)
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
			Help: "Detector findings by detector, rule, class, mode, route and source. In observe mode a block finding is counted and never denies. The route and the source are bounded, and no label holds request content.",
		}, []string{"detector", "rule", "class", "mode", "route", "source"}),
		failures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_egress_scan_detector_failures_total",
			Help: "Detector calls without a verdict by detector, reason (timeout, budget, oversize, error, panic), mode, route and source. In enforce mode with failure mode deny, each one denied a request.",
		}, []string{"detector", "reason", "mode", "route", "source"}),
	}
	registry.MustRegister(m.findings, m.failures)
	return m
}

func (m *detectorMetrics) record(host *detect.Host, v detect.Verdict, route, source string) {
	if m == nil {
		return
	}
	name, mode := host.Name(), string(host.Config().Mode)
	for _, f := range v.Findings {
		m.findings.WithLabelValues(name, f.RuleID, string(f.Class), mode, route, source).Inc()
	}
	if v.Failure != detect.FailureNone {
		m.failures.WithLabelValues(name, string(v.Failure), mode, route, source).Inc()
	}
}
