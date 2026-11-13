package telemetry

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func activeConfig() Config {
	return Config{Endpoint: "collector:4317", ServiceName: "test-service", ServiceVersion: "test-version", SamplerType: "always_on", SamplerArg: "1.0"}
}

func cleanEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{"OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME", "OTEL_SERVICE_VERSION", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG"} {
		t.Setenv(key, "")
	}
}

func TestEnvironmentDefaultsAndOverrides(t *testing.T) {
	cleanEnv(t)
	want := Config{ServiceName: "adapter", ServiceVersion: "dev", SamplerType: "parentbased_traceidratio", SamplerArg: "1.0"}
	if got := ConfigFromEnv("adapter"); !reflect.DeepEqual(got, want) {
		t.Fatalf("defaults = %#v, want %#v", got, want)
	}
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector:4317")
	t.Setenv("OTEL_SERVICE_NAME", "override")
	t.Setenv("OTEL_SERVICE_VERSION", "release")
	t.Setenv("OTEL_TRACES_SAMPLER", "always_off")
	t.Setenv("OTEL_TRACES_SAMPLER_ARG", "0.3")
	want = Config{Endpoint: "collector:4317", ServiceName: "override", ServiceVersion: "release", SamplerType: "always_off", SamplerArg: "0.3"}
	for _, disabled := range []string{"TRUE", "1", "false", "true"} {
		t.Setenv("OTEL_SDK_DISABLED", disabled)
		want.Disabled = disabled == "true"
		if got := ConfigFromEnv("adapter"); !reflect.DeepEqual(got, want) {
			t.Fatalf("overrides = %#v, want %#v", got, want)
		}
	}
}

func TestSamplersAndParents(t *testing.T) {
	for _, name := range []string{"always_on", "always_off", "traceidratio", "parentbased_always_on", "parentbased_always_off", "parentbased_traceidratio"} {
		for _, rate := range []float64{0, 1} {
			t.Run(name+"/"+string(rune('0'+int(rate))), func(t *testing.T) {
				sampler, err := samplerFor(name, rate)
				if err != nil {
					t.Fatal(err)
				}
				rootSample := strings.HasSuffix(name, "always_on") || (strings.HasSuffix(name, "traceidratio") && rate == 1)
				for _, parent := range []string{"root", "local-sampled", "local-unsampled", "remote-sampled", "remote-unsampled"} {
					ctx := context.Background()
					expected := rootSample
					if parent != "root" {
						sampled := strings.HasSuffix(parent, "-sampled")
						flags := trace.TraceFlags(0)
						if sampled {
							flags = trace.FlagsSampled
						}
						sc := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: flags, Remote: strings.HasPrefix(parent, "remote")})
						ctx = trace.ContextWithSpanContext(ctx, sc)
						if strings.HasPrefix(name, "parentbased_") {
							expected = sampled
						}
					}
					got := sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, TraceID: trace.TraceID{1}, Name: "probe"}).Decision
					want := sdktrace.Drop
					if expected {
						want = sdktrace.RecordAndSample
					}
					if got != want {
						t.Errorf("%s decision = %v, want %v", parent, got, want)
					}
				}
			})
		}
	}
}

func TestValidationBeforeAllocation(t *testing.T) {
	for _, tc := range []struct{ name, arg string }{{"always_on", "bad"}, {"unknown", "1"}, {"always_off", ""}} {
		cfg := activeConfig()
		cfg.SamplerType = tc.name
		cfg.SamplerArg = tc.arg
		calls := 0
		tp, err := Initialize(context.Background(), cfg, func(context.Context, string) (sdktrace.SpanExporter, error) {
			calls++
			return tracetest.NewInMemoryExporter(), nil
		}, func(trace.TracerProvider) { calls++ })
		if err == nil || tp != nil || calls != 0 {
			t.Fatalf("invalid config: provider=%v error=%v calls=%d", tp, err, calls)
		}
	}
}

func TestNumericArgumentsKeepSDKSemantics(t *testing.T) {
	for _, arg := range []string{"-1", "2", "NaN", "+Inf", "-Inf"} {
		cfg := activeConfig()
		cfg.SamplerType = "traceidratio"
		cfg.SamplerArg = arg
		tp, err := Initialize(context.Background(), cfg, func(context.Context, string) (sdktrace.SpanExporter, error) {
			return tracetest.NewInMemoryExporter(), nil
		}, func(trace.TracerProvider) {})
		if err != nil {
			t.Fatalf("argument %s: %v", arg, err)
		}
		if err := Shutdown(context.Background(), tp); err != nil {
			t.Fatal(err)
		}
	}
}

func TestDisabledAndAbsentEndpointDoNotInstall(t *testing.T) {
	before := otel.GetTracerProvider()
	for _, disabled := range []bool{false, true} {
		cfg := activeConfig()
		cfg.Disabled = disabled
		if !disabled {
			cfg.Endpoint = ""
		}
		cfg.SamplerArg = "invalid"
		cfg.SamplerType = "invalid"
		tp, err := Initialize(context.Background(), cfg, func(context.Context, string) (sdktrace.SpanExporter, error) {
			t.Fatal("exporter allocated")
			return nil, nil
		}, func(trace.TracerProvider) { t.Fatal("provider installed") })
		if err != nil || tp == nil {
			t.Fatalf("provider=%v error=%v", tp, err)
		}
		if otel.GetTracerProvider() != before {
			t.Fatal("global provider changed")
		}
		if err := Shutdown(context.Background(), tp); err != nil {
			t.Fatal(err)
		}
	}
}

func TestResourceIdentityAndBatchedExport(t *testing.T) {
	cfg := activeConfig()
	cfg.Attributes = []attribute.KeyValue{attribute.String("adapter.attribute", "present")}
	exporter := tracetest.NewInMemoryExporter()
	var installed trace.TracerProvider
	tp, err := Initialize(context.Background(), cfg, func(_ context.Context, endpoint string) (sdktrace.SpanExporter, error) {
		if endpoint != cfg.Endpoint {
			t.Fatal(endpoint)
		}
		return exporter, nil
	}, func(provider trace.TracerProvider) { installed = provider })
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = Shutdown(context.Background(), tp) })
	if installed != tp {
		t.Fatal("returned provider was not installed")
	}
	_, span := installed.Tracer("test").Start(context.Background(), "probe")
	span.End()
	if err := tp.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	spans := exporter.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("spans=%d", len(spans))
	}
	attrs := spans[0].Resource.Set()
	for key, want := range map[attribute.Key]string{"service.name": cfg.ServiceName, "service.version": cfg.ServiceVersion, "adapter.attribute": "present"} {
		got, ok := attrs.Value(key)
		if !ok || got.AsString() != want {
			t.Errorf("%s=%v, want %s", key, got, want)
		}
	}
	for _, attr := range resource.Default().Attributes() {
		if attr.Key == "service.name" || attr.Key == "service.version" {
			continue
		}
		got, ok := attrs.Value(attr.Key)
		if !ok || got != attr.Value {
			t.Errorf("missing default attribute %s", attr.Key)
		}
	}
}

func TestStartupErrorsDeadlineAndCancellation(t *testing.T) {
	sentinel := errors.New("exporter failed")
	for _, mode := range []string{"exporter-error", "cancelled"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if mode == "cancelled" {
				cancel()
			}
			want := sentinel
			if mode == "cancelled" {
				want = context.Canceled
			}
			tp, err := Initialize(ctx, activeConfig(), func(ctx context.Context, _ string) (sdktrace.SpanExporter, error) {
				deadline, ok := ctx.Deadline()
				remaining := time.Until(deadline)
				if !ok || remaining <= 9*time.Second || remaining > 10*time.Second {
					t.Fatalf("startup deadline remaining=%v", remaining)
				}
				if mode == "cancelled" {
					return nil, ctx.Err()
				}
				return nil, sentinel
			}, func(trace.TracerProvider) { t.Fatal("installed after exporter error") })
			if tp != nil || !errors.Is(err, want) {
				t.Fatalf("provider=%v error=%v", tp, err)
			}
		})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	tp, err := Initialize(ctx, activeConfig(), func(ctx context.Context, _ string) (sdktrace.SpanExporter, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}, func(trace.TracerProvider) { t.Fatal("installed after deadline") })
	if tp != nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("caller deadline: provider=%v error=%v", tp, err)
	}
}

type shutdownFunc func(context.Context) error

func (f shutdownFunc) Shutdown(ctx context.Context) error { return f(ctx) }

func TestShutdownBoundsAndErrors(t *testing.T) {
	if err := Shutdown(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if err := Shutdown(context.Background(), sdktrace.NewTracerProvider()); err != nil {
		t.Fatal(err)
	}
	sentinel := errors.New("shutdown failed")
	if err := shutdown(context.Background(), shutdownFunc(func(ctx context.Context) error {
		deadline, ok := ctx.Deadline()
		remaining := time.Until(deadline)
		if !ok || remaining <= 4*time.Second || remaining > 5*time.Second {
			t.Fatalf("shutdown deadline=%v", remaining)
		}
		return sentinel
	}), shutdownTimeout); !errors.Is(err, sentinel) {
		t.Fatalf("error=%v", err)
	}
	started := time.Now()
	err := shutdown(context.Background(), shutdownFunc(func(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }), 20*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > time.Second {
		t.Fatalf("bounded shutdown: %v elapsed=%v", err, time.Since(started))
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err = shutdown(ctx, shutdownFunc(func(ctx context.Context) error { return ctx.Err() }), shutdownTimeout)
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
