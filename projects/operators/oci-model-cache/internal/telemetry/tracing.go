/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package telemetry

import (
	"context"

	shared "github.com/jomcgi/homelab/projects/shared/telemetry"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// InitializeTracing configures tracing using the standard OTEL environment variables.
func InitializeTracing(ctx context.Context) (*sdktrace.TracerProvider, error) {
	return initializeTracing(ctx, newOTLPExporter, otel.SetTracerProvider)
}

func initializeTracing(ctx context.Context, exporter shared.ExporterFactory, install func(trace.TracerProvider)) (*sdktrace.TracerProvider, error) {
	cfg := shared.ConfigFromEnv("oci-model-cache-operator")
	cfg.Attributes = []attribute.KeyValue{attribute.String("k8s.operator.type", "custom-controller"), attribute.String("k8s.operator.name", "oci-model-cache-operator")}
	return shared.Initialize(ctx, cfg, exporter, install)
}

func newOTLPExporter(ctx context.Context, endpoint string) (sdktrace.SpanExporter, error) {
	return otlptracegrpc.New(ctx, exporterOptions(endpoint)...)
}

func exporterOptions(endpoint string) []otlptracegrpc.Option {
	return []otlptracegrpc.Option{otlptracegrpc.WithEndpoint(endpoint), otlptracegrpc.WithInsecure()}
}

// Shutdown flushes spans within the shared shutdown bound.
func Shutdown(ctx context.Context, tp *sdktrace.TracerProvider) error {
	return shared.Shutdown(ctx, tp)
}
