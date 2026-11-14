// Package metrics holds the connector's Prometheus metrics. Every label takes
// values from a fixed set or from configuration, never from run data.
package metrics

import "github.com/prometheus/client_golang/prometheus"

// Metrics is the connector's metric set.
type Metrics struct {
	Claims        *prometheus.CounterVec // agent, outcome
	Sends         *prometheus.CounterVec // kind, outcome
	Heartbeats    *prometheus.CounterVec // outcome
	Results       *prometheus.CounterVec // status
	Blocked       *prometheus.CounterVec // class
	Retries       *prometheus.CounterVec // class
	Cancellations *prometheus.CounterVec // reason, state
	Events        *prometheus.CounterVec // outcome
	Reassignments prometheus.Counter
	WriterEpoch   prometheus.Gauge
	TickErrors    *prometheus.CounterVec // phase
}

// New registers the metrics with reg; a nil reg leaves them unregistered.
func New(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		Claims: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_claims_total",
			Help: "itsaplan claims by agent key and outcome (new, redelivery, duplicate, empty, error).",
		}, []string{"agent", "outcome"}),
		Sends: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_sends_total",
			Help: "kagent sends by kind (initial, continuation) and outcome (accepted, ambiguous, not_accepted, not_found, reissued).",
		}, []string{"kind", "outcome"}),
		Heartbeats: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_heartbeats_total",
			Help: "itsaplan heartbeats by outcome (ok, not_found, error).",
		}, []string{"outcome"}),
		Results: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_results_total",
			Help: "Run results delivered to itsaplan by status.",
		}, []string{"status"}),
		Blocked: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_blocked_total",
			Help: "Work items blocked by error class (credential, quota).",
		}, []string{"class"}),
		Retries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_retries_total",
			Help: "Engineering retry decisions by outcome (sent, exhausted).",
		}, []string{"outcome"}),
		Cancellations: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_cancellations_total",
			Help: "kagent cancellations by reason and state (requested, confirmed).",
		}, []string{"reason", "state"}),
		Events: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_event_batches_total",
			Help: "itsaplan run event batches by outcome (ok, conflict, not_found, error).",
		}, []string{"outcome"}),
		Reassignments: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_connector_reassignments_total",
			Help: "Runs that moved a work item to another agent key.",
		}),
		WriterEpoch: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_connector_writer_epoch",
			Help: "The writer epoch this process holds, 0 when it is not the writer.",
		}),
		TickErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_tick_errors_total",
			Help: "Errors by dispatch phase (runs, cancellations, claims, outbox).",
		}, []string{"phase"}),
	}
	if reg != nil {
		reg.MustRegister(m.Claims, m.Sends, m.Heartbeats, m.Results, m.Blocked, m.Retries,
			m.Cancellations, m.Events, m.Reassignments, m.WriterEpoch, m.TickErrors)
	}
	return m
}
