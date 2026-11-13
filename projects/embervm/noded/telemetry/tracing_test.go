package telemetry

import (
	"context"
	"errors"
	"sync"
	"testing"

	shared "github.com/jomcgi/homelab/projects/shared/telemetry"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func TestInitializeTracingWithoutEndpointStaysDisabled(t *testing.T) {
	cfg := testConfig()
	cfg.Endpoint = ""
	factoryCalled := false
	installCalled := false

	tp, err := shared.Initialize(
		context.Background(),
		cfg,
		func(context.Context, string) (sdktrace.SpanExporter, error) {
			factoryCalled = true
			return nil, errors.New("unexpected exporter construction")
		},
		func(trace.TracerProvider) { installCalled = true },
	)
	if err != nil {
		t.Fatalf("initialize disabled tracing: %v", err)
	}
	if tp == nil {
		t.Fatal("disabled tracing returned a nil provider")
	}
	if factoryCalled {
		t.Fatal("disabled tracing constructed an exporter")
	}
	if installCalled {
		t.Fatal("disabled tracing replaced the global provider")
	}
	if err := tp.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown disabled provider: %v", err)
	}
}

func TestInitializeTracingRejectsInvalidSamplerBeforeCreatingExporter(t *testing.T) {
	cfg := testConfig()
	cfg.SamplerArg = "not-a-rate"
	factoryCalled := false
	installCalled := false

	tp, err := shared.Initialize(
		context.Background(),
		cfg,
		func(context.Context, string) (sdktrace.SpanExporter, error) {
			factoryCalled = true
			return nil, errors.New("unexpected exporter construction")
		},
		func(trace.TracerProvider) { installCalled = true },
	)
	if err == nil {
		t.Fatal("invalid sampler argument returned no error")
	}
	if tp != nil {
		t.Fatal("invalid sampler argument returned a provider")
	}
	if factoryCalled {
		t.Fatal("invalid sampler argument constructed an exporter")
	}
	if installCalled {
		t.Fatal("invalid sampler argument installed a provider")
	}
}

func TestInitializeTracingInstallsProviderExportsAndSetsResourceIdentity(t *testing.T) {
	cfg := testConfig()
	exporter := &recordingExporter{}
	// The driver creates its package tracer before main installs the provider.
	// Exercise that exact order so this test guards the existing call sites.
	driverTracer := otel.Tracer("embervm-noded/driver")

	tp, err := shared.Initialize(
		context.Background(),
		cfg,
		func(_ context.Context, endpoint string) (sdktrace.SpanExporter, error) {
			if endpoint != cfg.Endpoint {
				t.Fatalf("exporter endpoint = %q, want %q", endpoint, cfg.Endpoint)
			}
			return exporter, nil
		},
		otel.SetTracerProvider,
	)
	if err != nil {
		t.Fatalf("initialize tracing: %v", err)
	}

	_, span := driverTracer.Start(context.Background(), "provision_rootfs")
	span.End()
	if err := tp.ForceFlush(context.Background()); err != nil {
		t.Fatalf("flush span: %v", err)
	}

	spans := exporter.exportedSpans()
	if len(spans) != 1 {
		t.Fatalf("exported %d spans, want 1", len(spans))
	}
	if spans[0].Name() != "provision_rootfs" {
		t.Fatalf("span name = %q, want provision_rootfs", spans[0].Name())
	}
	attrs := spans[0].Resource().Set()
	serviceName, ok := attrs.Value("service.name")
	if !ok || serviceName.AsString() != cfg.ServiceName {
		t.Fatalf("service.name = %q (present %t), want %q", serviceName.AsString(), ok, cfg.ServiceName)
	}
	serviceVersion, ok := attrs.Value("service.version")
	if !ok || serviceVersion.AsString() != cfg.ServiceVersion {
		t.Fatalf("service.version = %q (present %t), want %q", serviceVersion.AsString(), ok, cfg.ServiceVersion)
	}

	if err := Shutdown(context.Background(), tp); err != nil {
		t.Fatalf("shutdown tracing: %v", err)
	}
	if !exporter.wasShutdown() {
		t.Fatal("provider shutdown did not shut down exporter")
	}
}

func testConfig() shared.Config {
	return shared.Config{
		Endpoint:       "http://collector.example.test:4317",
		ServiceName:    "embervm-noded-test",
		ServiceVersion: "test-version",
		SamplerType:    "always_on",
		SamplerArg:     "1.0",
	}
}

type recordingExporter struct {
	mu       sync.Mutex
	spans    []sdktrace.ReadOnlySpan
	shutdown bool
}

func (e *recordingExporter) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.spans = append(e.spans, spans...)
	return nil
}

func (e *recordingExporter) Shutdown(context.Context) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.shutdown = true
	return nil
}

func (e *recordingExporter) exportedSpans() []sdktrace.ReadOnlySpan {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]sdktrace.ReadOnlySpan(nil), e.spans...)
}

func (e *recordingExporter) wasShutdown() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.shutdown
}
