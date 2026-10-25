package meter

import (
	"context"
	"errors"
	"net/url"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
)

const ServiceName = "scotland-connector"

type ExportConfig struct {
	Enabled  bool
	Endpoint string
	Instance string
}

func (c ExportConfig) Validate() error {
	if !c.Enabled {
		return nil
	}
	u, err := url.Parse(c.Endpoint)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("meter export requires an explicit OTLP HTTP endpoint URL without credentials or query")
	}
	return nil
}

type otelSignals struct {
	calls, results, units metric.Int64Counter
	latency               metric.Float64Histogram
}

// StartExport is called once before serving. The provider is private to this
// meter, never global. Record only updates SDK memory; periodic export owns I/O.
// Cumulative sums preserve increments across exporter outages. Consumers must
// rate/difference each service.instance.id and start timestamp before summing
// replicas, never sum repeated cumulative samples. Restarts create a new ID.
func (m *Meter) StartExport(ctx context.Context, c ExportConfig) (func(context.Context) error, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	if !c.Enabled {
		return func(context.Context) error { return nil }, nil
	}
	exp, err := otlpmetrichttp.New(ctx,
		otlpmetrichttp.WithEndpointURL(c.Endpoint),
		otlpmetrichttp.WithTimeout(3*time.Second),
		otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: false}),
		otlpmetrichttp.WithTemporalitySelector(func(sdkmetric.InstrumentKind) metricdata.Temporality { return metricdata.CumulativeTemporality }))
	if err != nil {
		return nil, errors.New("initialize metric exporter")
	}
	id := uuid.NewString()
	if c.Instance != "" {
		id = c.Instance + "/" + id
	}
	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(resource.NewSchemaless(attribute.String("service.name", ServiceName), attribute.String("service.instance.id", id))),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(30*time.Second), sdkmetric.WithTimeout(3*time.Second))))
	om := provider.Meter("scotland-connector/meter")
	s := &otelSignals{}
	s.calls, _ = om.Int64Counter("scotland_connector_calls_total")
	s.results, _ = om.Int64Counter("scotland_connector_results_total")
	s.units, _ = om.Int64Counter("scotland_connector_invocation_units_total")
	s.latency, _ = om.Float64Histogram("scotland_connector_call_duration_seconds", metric.WithUnit("s"), metric.WithExplicitBucketBoundaries(0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05))
	if m.Cache != nil {
		names := []string{"hits", "eligible", "ratio", "window_start_seconds", "window_end_seconds", "observed_at_seconds", "sampled_estimate"}
		gauges := make([]metric.Float64ObservableGauge, len(names))
		instruments := make([]metric.Observable, len(names))
		for i, n := range names {
			gauges[i], _ = om.Float64ObservableGauge("scotland_connector_cache_" + n)
			instruments[i] = gauges[i]
		}
		_, _ = om.RegisterCallback(func(_ context.Context, observer metric.Observer) error {
			o, ok := m.Cache.Current(time.Now())
			if !ok {
				return nil
			}
			values := []float64{o.Hits, o.Eligible, o.Ratio, float64(o.Start.Unix()), float64(o.End.Unix()), float64(o.ObservedAt.Unix()), 1}
			for i, v := range values {
				observer.ObserveFloat64(gauges[i], v)
			}
			return nil
		}, instruments...)
	}
	m.otel = s
	m.flush = provider.ForceFlush
	return func(ctx context.Context) error {
		bounded, cancel := context.WithTimeout(ctx, 4*time.Second)
		defer cancel()
		return provider.Shutdown(bounded)
	}, nil
}

// ForceFlush is an optional administrative lifecycle flush, never used by a
// public handler. The caller deadline and the exporter timeout bound its I/O.
func (m *Meter) ForceFlush(ctx context.Context) error {
	if m.flush == nil {
		return nil
	}
	bounded, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	return m.flush(bounded)
}

func (s *otelSignals) record(tool, surface, class, outcome, unit string, units int64, results int, d time.Duration) {
	if s == nil {
		return
	}
	ctx := context.Background()
	base := []attribute.KeyValue{attribute.String("tool", tool), attribute.String("surface", surface)}
	s.latency.Record(ctx, d.Seconds(), metric.WithAttributes(base...))
	base = append(base, attribute.String("client_class", class))
	if results > 0 {
		s.results.Add(ctx, int64(results), metric.WithAttributes(base...))
	}
	if units > 0 {
		s.units.Add(ctx, units, metric.WithAttributes(append(base, attribute.String("unit", unit))...))
	}
	s.calls.Add(ctx, 1, metric.WithAttributes(append(base, attribute.String("outcome", outcome))...))
}
