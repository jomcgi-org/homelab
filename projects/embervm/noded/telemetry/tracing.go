package telemetry

import (
	"context"

	shared "github.com/jomcgi/homelab/projects/shared/telemetry"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// InitializeTracing configures tracing using the standard OTEL environment variables.
func InitializeTracing(ctx context.Context) (*sdktrace.TracerProvider, error) {
	return initializeTracing(ctx, newOTLPExporter, otel.SetTracerProvider)
}

func initializeTracing(ctx context.Context, exporter shared.ExporterFactory, install func(trace.TracerProvider)) (*sdktrace.TracerProvider, error) {
	cfg := shared.ConfigFromEnv("embervm-noded")
	return shared.Initialize(ctx, cfg, exporter, install)
}

func newOTLPExporter(ctx context.Context, endpoint string) (sdktrace.SpanExporter, error) {
	return otlptracegrpc.New(ctx, exporterOptions(endpoint)...)
}

func exporterOptions(endpoint string) []otlptracegrpc.Option {
	return []otlptracegrpc.Option{otlptracegrpc.WithEndpointURL(endpoint)}
}

// Shutdown flushes spans within the shared shutdown bound.
func Shutdown(ctx context.Context, tp *sdktrace.TracerProvider) error {
	return shared.Shutdown(ctx, tp)
}
