package telemetry

import (
	"context"
	"testing"

	"github.com/jomcgi/homelab/projects/shared/telemetry/testutil"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestAdapterEndpointOptions(t *testing.T) {
	for _, tc := range []struct {
		name, endpoint, address string
		tls                     bool
	}{
		{"host-port", "operator.example.test:4317", "operator.example.test:4317", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Explicit adapter options must override both generic and trace-specific SDK settings.
			t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://generic.invalid:123")
			t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://trace.invalid:456")
			t.Setenv("OTEL_EXPORTER_OTLP_INSECURE", "false")
			t.Setenv("OTEL_EXPORTER_OTLP_TRACES_INSECURE", "true")
			testutil.CheckEndpoint(t, exporterOptions(tc.endpoint), tc.address, tc.tls)
		})
	}
}

func TestAdapterActiveIdentityDefaultsAndOverrides(t *testing.T) {
	for _, override := range []bool{false, true} {
		name := "defaults"
		if override {
			name = "overrides"
		}
		t.Run(name, func(t *testing.T) {
			for _, key := range []string{"OTEL_SDK_DISABLED", "OTEL_SERVICE_NAME", "OTEL_SERVICE_VERSION", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG"} {
				t.Setenv(key, "")
			}
			t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "operator.example.test:4317")
			wantName, wantVersion := "oci-model-cache-operator", "dev"
			if override {
				wantName = "override"
				wantVersion = "release"
				t.Setenv("OTEL_SERVICE_NAME", wantName)
				t.Setenv("OTEL_SERVICE_VERSION", wantVersion)
			}
			exporter := tracetest.NewInMemoryExporter()
			var installed trace.TracerProvider
			tp, err := initializeTracing(context.Background(), func(_ context.Context, endpoint string) (sdktrace.SpanExporter, error) {
				if endpoint != "operator.example.test:4317" {
					t.Fatal(endpoint)
				}
				return exporter, nil
			}, func(provider trace.TracerProvider) { installed = provider })
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = Shutdown(context.Background(), tp) })
			if installed != tp {
				t.Fatal("active initialization did not install returned provider")
			}
			_, span := tp.Tracer("adapter-test").Start(context.Background(), "probe")
			span.End()
			if err := tp.ForceFlush(context.Background()); err != nil {
				t.Fatal(err)
			}
			spans := exporter.GetSpans()
			if len(spans) != 1 {
				t.Fatalf("spans=%d", len(spans))
			}
			attrs := spans[0].Resource.Set()
			for key, want := range map[attribute.Key]string{"service.name": wantName, "service.version": wantVersion, "k8s.operator.type": "custom-controller", "k8s.operator.name": "oci-model-cache-operator"} {
				got, ok := attrs.Value(key)
				if !ok || got.AsString() != want {
					t.Errorf("%s=%v, want %s", key, got, want)
				}
			}
		})
	}
}
