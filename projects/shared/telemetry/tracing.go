package telemetry

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const (
	startupTimeout  = 10 * time.Second
	shutdownTimeout = 5 * time.Second
)

// Config holds tracing settings and service-specific resource attributes.
type Config struct {
	Attributes     []attribute.KeyValue
	Disabled       bool
	Endpoint       string
	ServiceName    string
	ServiceVersion string
	SamplerType    string
	SamplerArg     string
}

// ExporterFactory allocates an exporter after configuration validation.
type (
	ExporterFactory   func(context.Context, string) (sdktrace.SpanExporter, error)
	ProviderInstaller func(trace.TracerProvider)
)

// ConfigFromEnv reads the standard OTEL settings with the adapter service identity.
func ConfigFromEnv(defaultServiceName string) Config {
	return Config{
		Disabled:       os.Getenv("OTEL_SDK_DISABLED") == "true",
		Endpoint:       os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
		ServiceName:    valueOrDefault(os.Getenv("OTEL_SERVICE_NAME"), defaultServiceName),
		ServiceVersion: valueOrDefault(os.Getenv("OTEL_SERVICE_VERSION"), "dev"),
		SamplerType:    valueOrDefault(os.Getenv("OTEL_TRACES_SAMPLER"), "parentbased_traceidratio"),
		SamplerArg:     valueOrDefault(os.Getenv("OTEL_TRACES_SAMPLER_ARG"), "1.0"),
	}
}

// Initialize validates configuration, creates a batched provider and installs it.
func Initialize(
	ctx context.Context,
	cfg Config,
	newExporter ExporterFactory,
	install ProviderInstaller,
) (*sdktrace.TracerProvider, error) {
	if cfg.Disabled || cfg.Endpoint == "" {
		return sdktrace.NewTracerProvider(), nil
	}

	sampleRate, err := strconv.ParseFloat(cfg.SamplerArg, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid OTEL_TRACES_SAMPLER_ARG %q: %w", cfg.SamplerArg, err)
	}

	sampler, err := samplerFor(cfg.SamplerType, sampleRate)
	if err != nil {
		return nil, err
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewSchemaless(append([]attribute.KeyValue{
			attribute.String("service.name", cfg.ServiceName),
			attribute.String("service.version", cfg.ServiceVersion),
		}, cfg.Attributes...)...),
	)
	if err != nil {
		return nil, fmt.Errorf("create tracing resource: %w", err)
	}

	exporterCtx, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()
	exporter, err := newExporter(exporterCtx, cfg.Endpoint)
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
