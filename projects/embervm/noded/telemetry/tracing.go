package telemetry

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const (
	defaultServiceName    = "embervm-noded"
	defaultServiceVersion = "dev"
	startupTimeout        = 10 * time.Second
	shutdownTimeout       = 5 * time.Second
)

type tracingConfig struct {
	disabled       bool
	endpoint       string
	serviceName    string
	serviceVersion string
	samplerType    string
	samplerArg     string
}

type (
	exporterFactory   func(context.Context, string) (sdktrace.SpanExporter, error)
	providerInstaller func(trace.TracerProvider)
)

// InitializeTracing configures OTLP/gRPC tracing from the standard OTEL
// environment variables. An unset endpoint or OTEL_SDK_DISABLED=true keeps
// tracing disabled and does not replace the global no-op provider.
func InitializeTracing(ctx context.Context) (*sdktrace.TracerProvider, error) {
	return initializeTracing(ctx, configFromEnv(), newOTLPExporter, otel.SetTracerProvider)
}

func configFromEnv() tracingConfig {
	return tracingConfig{
		disabled:       os.Getenv("OTEL_SDK_DISABLED") == "true",
		endpoint:       os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
		serviceName:    valueOrDefault(os.Getenv("OTEL_SERVICE_NAME"), defaultServiceName),
		serviceVersion: valueOrDefault(os.Getenv("OTEL_SERVICE_VERSION"), defaultServiceVersion),
		samplerType:    valueOrDefault(os.Getenv("OTEL_TRACES_SAMPLER"), "parentbased_traceidratio"),
		samplerArg:     valueOrDefault(os.Getenv("OTEL_TRACES_SAMPLER_ARG"), "1.0"),
	}
}

func initializeTracing(
	ctx context.Context,
	cfg tracingConfig,
	newExporter exporterFactory,
	install providerInstaller,
) (*sdktrace.TracerProvider, error) {
	if cfg.disabled || cfg.endpoint == "" {
		return sdktrace.NewTracerProvider(), nil
	}

	sampleRate, err := strconv.ParseFloat(cfg.samplerArg, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid OTEL_TRACES_SAMPLER_ARG %q: %w", cfg.samplerArg, err)
	}

	sampler, err := samplerFor(cfg.samplerType, sampleRate)
	if err != nil {
		return nil, err
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewSchemaless(
			attribute.String("service.name", cfg.serviceName),
			attribute.String("service.version", cfg.serviceVersion),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("create tracing resource: %w", err)
	}

	exporterCtx, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()
	exporter, err := newExporter(exporterCtx, cfg.endpoint)
	if err != nil {
		return nil, fmt.Errorf("create OTLP trace exporter: %w", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sampler),
	)
	install(tp)
	return tp, nil
}

func newOTLPExporter(ctx context.Context, endpoint string) (sdktrace.SpanExporter, error) {
	return otlptracegrpc.New(ctx, otlptracegrpc.WithEndpointURL(endpoint))
}

func samplerFor(name string, sampleRate float64) (sdktrace.Sampler, error) {
	switch name {
	case "always_on":
		return sdktrace.AlwaysSample(), nil
	case "always_off":
		return sdktrace.NeverSample(), nil
	case "traceidratio":
		return sdktrace.TraceIDRatioBased(sampleRate), nil
	case "parentbased_always_on":
		return sdktrace.ParentBased(sdktrace.AlwaysSample()), nil
	case "parentbased_always_off":
		return sdktrace.ParentBased(sdktrace.NeverSample()), nil
	case "parentbased_traceidratio":
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(sampleRate)), nil
	default:
		return nil, fmt.Errorf("unknown OTEL_TRACES_SAMPLER: %s", name)
	}
}

// Shutdown flushes pending spans and stops the provider within a fixed bound.
func Shutdown(ctx context.Context, tp *sdktrace.TracerProvider) error {
	if tp == nil {
		return nil
	}
	return shutdown(ctx, tp, shutdownTimeout)
}

type shutdowner interface {
	Shutdown(context.Context) error
}

func shutdown(ctx context.Context, tp shutdowner, timeout time.Duration) error {
	shutdownCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if err := tp.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("shutdown tracer provider: %w", err)
	}
	return nil
}

func valueOrDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
