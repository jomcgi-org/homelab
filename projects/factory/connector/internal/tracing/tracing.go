package tracing

import (
	"context"
	"errors"
	"os"
	"sort"
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdk "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func Init(ctx context.Context) (*sdk.TracerProvider, error) {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" && os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "" {
		return nil, nil
	}
	exp, err := otlptracegrpc.New(ctx)
	if err != nil {
		return nil, err
	}
	tp := sdk.NewTracerProvider(sdk.WithBatcher(exp))
	otel.SetTracerProvider(tp)
	return tp, nil
}

func Start(ctx context.Context, name string, options ...trace.SpanStartOption) (context.Context, trace.Span) {
	if parent := trace.SpanContextFromContext(ctx); parent.IsValid() {
		options = append(options, trace.WithLinks(trace.Link{SpanContext: parent}))
	}
	return otel.Tracer("factory-connector").Start(ctx, name, append(options, trace.WithNewRoot())...)
}

// Error messages from upstreams may contain credentials or response bodies.
func Finish(span trace.Span, err error, outcome string) {
	if err != nil {
		outcome = "error"
		span.RecordError(errors.New("connector operation failed"))
		span.SetStatus(codes.Error, "connector operation failed")
	}
	span.SetAttributes(attribute.String("outcome", outcome))
	span.AddEvent(outcome)
	span.End()
}

// A poll can handle multiple issues, runs and PRs. Keep their identifiers
// searchable on the root and preserve their associations on individual events.
type (
	pollKey  struct{}
	pollWork struct {
		mu     sync.Mutex
		span   trace.Span
		values map[attribute.Key]map[string]struct{}
	}
)

func StartPoll(ctx context.Context) (context.Context, trace.Span) {
	ctx, span := Start(ctx, "factory.poll")
	if span.IsRecording() {
		ctx = context.WithValue(ctx, pollKey{}, &pollWork{span: span, values: make(map[attribute.Key]map[string]struct{})})
	}
	return ctx, span
}

func Observe(ctx context.Context, event string, attrs ...attribute.KeyValue) {
	work, _ := ctx.Value(pollKey{}).(*pollWork)
	if work == nil {
		return
	}
	work.mu.Lock()
	defer work.mu.Unlock()
	work.span.AddEvent(event, trace.WithAttributes(attrs...))
	for _, a := range attrs {
		values := work.values[a.Key]
		if values == nil {
			values = map[string]struct{}{}
			work.values[a.Key] = values
		}
		values[a.Value.Emit()] = struct{}{}
	}
}

func FinishPoll(ctx context.Context, span trace.Span, err error) {
	if work, ok := ctx.Value(pollKey{}).(*pollWork); ok {
		work.mu.Lock()
		for k, values := range work.values {
			list := make([]string, 0, len(values))
			for v := range values {
				list = append(list, v)
			}
			sort.Strings(list)
			span.SetAttributes(attribute.StringSlice("poll."+string(k), list))
		}
		work.mu.Unlock()
	}
	Finish(span, err, "completed")
}
