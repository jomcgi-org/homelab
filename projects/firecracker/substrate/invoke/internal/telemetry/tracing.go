// Package telemetry wires a global OpenTelemetry TracerProvider, an OTLP gRPC
// exporter, and a W3C trace-context propagator for the fc-invoke daemon, so the
// spans the driver already creates (provision_rootfs, firecracker_boot) reach
// the SigNoz collector instead of a no-op tracer.
//
// It degrades gracefully: with no OTEL_EXPORTER_OTLP_ENDPOINT set (local builds,
// CI, any environment without a collector) it installs a plain no-op provider
// and returns, so tracing never blocks the daemon from running. This mirrors
// projects/operators/oci-model-cache/internal/telemetry.
package telemetry

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// InitTracing sets up OpenTelemetry tracing from standard OTEL environment
// variables and installs the resulting provider and propagator globally.
//
// If OTEL_EXPORTER_OTLP_ENDPOINT is empty (or OTEL_SDK_DISABLED=true) it returns
// a no-op TracerProvider so spans are simply discarded and the daemon keeps
// running. The returned provider is always non-nil on a nil error.
func InitTracing(ctx context.Context) (*sdktrace.TracerProvider, error) {
	if os.Getenv("OTEL_SDK_DISABLED") == "true" {
		slog.Info("OpenTelemetry tracing disabled (OTEL_SDK_DISABLED=true)")
		return sdktrace.NewTracerProvider(), nil
	}

	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		slog.Info("OpenTelemetry tracing disabled (no OTEL_EXPORTER_OTLP_ENDPOINT set)")
		return sdktrace.NewTracerProvider(), nil
	}

	serviceName := os.Getenv("OTEL_SERVICE_NAME")
	if serviceName == "" {
		serviceName = "fc-invoke"
	}

	serviceVersion := os.Getenv("OTEL_SERVICE_VERSION")
	if serviceVersion == "" {
		serviceVersion = "dev"
	}

	samplerType := os.Getenv("OTEL_TRACES_SAMPLER")
	if samplerType == "" {
		samplerType = "parentbased_always_on"
	}

	samplerArg := os.Getenv("OTEL_TRACES_SAMPLER_ARG")
	if samplerArg == "" {
		samplerArg = "1.0"
	}

	sampleRate, err := strconv.ParseFloat(samplerArg, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid OTEL_TRACES_SAMPLER_ARG %q: %w", samplerArg, err)
	}

	exporterCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	exporter, err := otlptracegrpc.New(exporterCtx,
		otlptracegrpc.WithEndpoint(endpoint),
		otlptracegrpc.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create OTLP exporter: %w", err)
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewSchemaless(
			attribute.String("service.name", serviceName),
			attribute.String("service.version", serviceVersion),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create resource: %w", err)
	}

	var sampler sdktrace.Sampler
	switch samplerType {
	case "always_on":
		sampler = sdktrace.AlwaysSample()
	case "always_off":
		sampler = sdktrace.NeverSample()
	case "traceidratio":
		sampler = sdktrace.TraceIDRatioBased(sampleRate)
	case "parentbased_always_on":
		sampler = sdktrace.ParentBased(sdktrace.AlwaysSample())
	case "parentbased_always_off":
		sampler = sdktrace.ParentBased(sdktrace.NeverSample())
	case "parentbased_traceidratio":
		sampler = sdktrace.ParentBased(sdktrace.TraceIDRatioBased(sampleRate))
	default:
		return nil, fmt.Errorf("unknown OTEL_TRACES_SAMPLER: %s", samplerType)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sampler),
	)

	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	slog.Info("OpenTelemetry tracing enabled",
		"endpoint", endpoint,
		"serviceName", serviceName,
		"serviceVersion", serviceVersion,
		"sampler", samplerType,
	)

	return tp, nil
}

// Shutdown flushes and stops the tracer provider, bounded to 5 seconds so a
// slow or unreachable collector cannot stall daemon shutdown.
func Shutdown(ctx context.Context, tp *sdktrace.TracerProvider) error {
	if tp == nil {
		return nil
	}

	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	if err := tp.Shutdown(ctx); err != nil {
		return fmt.Errorf("failed to shutdown tracer provider: %w", err)
	}

	return nil
}
