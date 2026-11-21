// Package metrics holds the connector's Prometheus metrics. Every label takes
// values from a fixed set or from configuration, never from run data.
package metrics

import "github.com/prometheus/client_golang/prometheus"

// Metrics is the connector's metric set.
type Metrics struct {
	Claims              *prometheus.CounterVec // agent, outcome
	Sends               *prometheus.CounterVec // kind, outcome
	Heartbeats          *prometheus.CounterVec // outcome
	Results             *prometheus.CounterVec // status
	Blocked             *prometheus.CounterVec // class
	Retries             *prometheus.CounterVec // class
	Cancellations       *prometheus.CounterVec // reason, state
	Events              *prometheus.CounterVec // outcome
	DeferredStarted     prometheus.Counter
	Reassignments       prometheus.Counter
	WriterEpoch         prometheus.Gauge
	ItsaplanUnavailable *prometheus.GaugeVec
	TickErrors          *prometheus.CounterVec // phase
	HeldRuns            prometheus.Gauge
	OldestHold          prometheus.Gauge

	PullRequests      *prometheus.CounterVec // outcome
	ReviewEvents      *prometheus.CounterVec // kind, disposition
	ReviewRounds      prometheus.Counter
	RebaseRounds      *prometheus.CounterVec // outcome
	SplitRounds       *prometheus.CounterVec // outcome
	IdleRounds        prometheus.Counter
	MergeQueue        *prometheus.CounterVec // outcome
	CILogReads        *prometheus.CounterVec // outcome
	GoalReasonReads   *prometheus.CounterVec // outcome
	SessionRecoveries *prometheus.CounterVec // kind
	SessionsDeleted   *prometheus.CounterVec // reason
	StuckTurns        *prometheus.CounterVec // step

	CredentialRefusals *prometheus.CounterVec // role, kind
}

// DispatchSnapshot holds the queue gauges for a single scrape.
type DispatchSnapshot struct {
	BusyRuns       *prometheus.GaugeVec
	DispatchedRuns *prometheus.GaugeVec
	RunnableRuns   prometheus.Gauge
}

func NewDispatchSnapshot() *DispatchSnapshot {
	return &DispatchSnapshot{
		BusyRuns:       prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "factory_connector_busy_runs", Help: "Runs occupying scheduler slots by agent."}, []string{"agent"}),
		DispatchedRuns: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "factory_connector_dispatched_runs", Help: "Runs currently dispatched by agent."}, []string{"agent"}),
		RunnableRuns:   prometheus.NewGauge(prometheus.GaugeOpts{Name: "factory_connector_runnable_runs", Help: "Queued runs without an admission, work item or grant block."}),
	}
}

// New registers the metrics with reg; a nil reg leaves them unregistered.
func New(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		HeldRuns: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_connector_held_runs",
			Help: "Runs waiting for admission (issue brief or dependencies).",
		}),
		OldestHold: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_connector_oldest_hold_seconds",
			Help: "Age of the oldest current admission hold, zero when none are held.",
		}),
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
			Help: "kagent cancellations by reason and state (requested, confirmed, withdrawn).",
		}, []string{"reason", "state"}),
		Events: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_event_batches_total",
			Help: "itsaplan run event batches by outcome (ok, conflict, not_found, error).",
		}, []string{"outcome"}),
		DeferredStarted: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_connector_deferred_started_total",
			Help: "Deferred issues delegated on or after their UTC start date.",
		}),
		Reassignments: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_connector_reassignments_total",
			Help: "Runs that moved a work item to another agent key.",
		}),
		WriterEpoch: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_connector_writer_epoch",
			Help: "The writer epoch this process holds, 0 when it is not the writer.",
		}),
		ItsaplanUnavailable: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "factory_connector_itsaplan_unavailable", Help: "One while a dispatch phase is failing on itsaplan.",
		}, []string{"phase"}),
		TickErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_tick_errors_total",
			Help: "Errors by dispatch phase (heartbeats, deferred, claims, quota, admissions, runs, cancellations, delivery, outbox, sessions, admission_metrics).",
		}, []string{"phase"}),
		PullRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_pull_requests_total",
			Help: "PR syncs after a finished turn by outcome (created, adopted, updated, no_commits).",
		}, []string{"outcome"}),
		ReviewEvents: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_review_events_total",
			Help: "Review events recorded by kind and disposition (acted, stale_head, untrusted, duplicate, round_limit).",
		}, []string{"kind", "disposition"}),
		ReviewRounds: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_connector_review_rounds_total",
			Help: "Review rounds the connector started.",
		}),
		RebaseRounds: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_rebase_rounds_total",
			Help: "Rebase round outcomes (started, idle, limited). Limited counts once per work item.",
		}, []string{"outcome"}),
		SplitRounds: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_split_rounds_total",
			Help: "Split round outcomes (started, idle, limited) for PRs over the reviewer's size budget. Limited counts once per work item.",
		}, []string{"outcome"}),
		IdleRounds: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_connector_idle_review_rounds_total",
			Help: "Review rounds that closed with the PR still at the head they were asked to change.",
		}),
		CILogReads: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_ci_log_reads_total",
			Help: "CI invocation log reads for revision prompts by outcome (ok, or the failure class).",
		}, []string{"outcome"}),
		GoalReasonReads: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_goal_reason_reads_total",
			Help: "Reads of the reviewer's goal check reasons for revision rounds by outcome (ok, none, other_issue, error).",
		}, []string{"outcome"}),
		MergeQueue: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_merge_queue_total",
			Help: "Merge queue outcomes (enqueued, enqueue_error, ejected, merged).",
		}, []string{"outcome"}),
		SessionRecoveries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_session_recoveries_total",
			Help: "kagent session recoveries by kind (revert, replace).",
		}, []string{"kind"}),
		SessionsDeleted: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_sessions_deleted_total",
			Help: "kagent sessions the connector deleted by reason: its work item is done or stopped, or it was replaced.",
		}, []string{"reason"}),
		StuckTurns: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_stuck_turns_total",
			Help: "Steps that free a turn past its liveness lease (HOME-141): lease, not_started, force_delete, " +
				"refused, revert, deleted.",
		}, []string{"step"}),
		CredentialRefusals: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_connector_credential_refusals_total",
			Help: "itsaplan 401 and 403 answers by key role (agent, coordinator) and kind: an outbox kind " +
				"(result, comment, issue_done, deferred_start), a lookup (admission, replacement, brief, issue_check, run_check, deferred_poll) or a runner call " +
				"(claim, heartbeat, events).",
		}, []string{"role", "kind"}),
	}
	// Establish the counter baseline before the first successful dispatch.
	m.Sends.WithLabelValues("initial", "accepted")
	m.Sends.WithLabelValues("continuation", "accepted")
	if reg != nil {
		reg.MustRegister(m.Claims, m.Sends, m.Heartbeats, m.Results, m.Blocked, m.Retries,
			m.DeferredStarted, m.Cancellations, m.Events, m.Reassignments, m.WriterEpoch, m.ItsaplanUnavailable, m.TickErrors,
			m.PullRequests, m.ReviewEvents, m.ReviewRounds, m.RebaseRounds, m.SplitRounds, m.IdleRounds, m.MergeQueue, m.CILogReads, m.GoalReasonReads, m.SessionRecoveries, m.SessionsDeleted, m.StuckTurns, m.CredentialRefusals,
			m.HeldRuns, m.OldestHold)
	}
	return m
}
