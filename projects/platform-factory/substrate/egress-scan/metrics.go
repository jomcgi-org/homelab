package main

import (
	"context"
	"errors"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
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
	checks := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "factory_egress_scan_checks_total",
		Help: "Completed egress authorization checks by verdict.",
	}, []string{"outcome"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "factory_egress_scan_check_duration_seconds",
		Help:    "Time in the egress authorization handler, including request decoding.",
		Buckets: []float64{.001, .0025, .005, .01, .025, .05, .075, .08, .1, .25},
	}, []string{"outcome"})
	for _, outcome := range []string{"allowed", "credential_blocked", "invalid_request", "capacity_limited", "work_limit", "cancelled", "error"} {
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
