// Package metrics holds the reviewer's Prometheus metrics. Every label takes
// values from a fixed set or from configuration (the binding name), never
// from PR or review data.
package metrics

import "github.com/prometheus/client_golang/prometheus"

// Metrics is the reviewer's metric set.
type Metrics struct {
	ReviewsStarted   *prometheus.CounterVec // binding, kind
	ReviewsCompleted *prometheus.CounterVec // binding, conclusion
	Findings         *prometheus.CounterVec // binding, severity, rule
	Withdrawals      *prometheus.CounterVec // binding, severity, rule
	HumanGates       *prometheus.CounterVec // binding
	Retries          *prometheus.CounterVec // binding
	SplitRequests    *prometheus.CounterVec // binding
	InFlight         prometheus.Gauge
	Enqueues         *prometheus.CounterVec // binding, outcome
	MergeGroupChecks *prometheus.CounterVec // binding, conclusion
	GitHubErrors     *prometheus.CounterVec // binding, call
	KagentErrors     *prometheus.CounterVec // binding, call
	Conditional      *prometheus.CounterVec // result
	CommentWrites    *prometheus.CounterVec // binding, outcome

	// Session paths and usage of reviews (review/session.go).
	ReviewSessions  *prometheus.CounterVec   // binding, path, reason
	ReviewTaskBytes *prometheus.HistogramVec // binding, path
	ReviewTokens    *prometheus.CounterVec   // binding, path, kind
	ReviewUsage     *prometheus.CounterVec   // binding, path, usage
}

// New registers the metrics with reg; a nil reg leaves them unregistered.
func New(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		ReviewsStarted: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_reviews_started_total",
			Help: "Reviews started by binding and kind (new: check run created and task sent; reattach: an in-progress check run picked up again).",
		}, []string{"binding", "kind"}),
		ReviewsCompleted: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_reviews_completed_total",
			Help: "Review check runs completed by binding and conclusion (success, failure, cancelled), including human gate changes.",
		}, []string{"binding", "conclusion"}),
		Findings: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_findings_total",
			Help: "Findings in completed reviews by binding, severity (critical, high, medium, low) and rule (a configured rule ID, " +
				"human-gate, or other). Each critical one is also logged as \"critical finding\".",
		}, []string{"binding", "severity", "rule"}),
		Withdrawals: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_withdrawals_total",
			Help: "Previous findings the reviewer withdrew as mistaken, by binding, severity and rule. A withdrawn high or " +
				"critical one is also logged as \"severe finding withdrawn\".",
		}, []string{"binding", "severity", "rule"}),
		HumanGates: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_human_gates_total",
			Help: "Heads held for a human review by path or label, by binding; each is also logged as \"human gate\".",
		}, []string{"binding"}),
		Retries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_retries_total",
			Help: "Review attempts that ended without a report (a failed, rejected or timed-out task, an unusable reply, " +
				"or reads that kept failing) and were followed by another attempt on the same head, by binding.",
		}, []string{"binding"}),
		SplitRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_split_requests_total",
			Help: "PRs failed without a review for exceeding the diff budget, by binding.",
		}, []string{"binding"}),
		InFlight: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_reviewer_reviews_in_flight",
			Help: "Reviews this process is running, bounded by the configured concurrency.",
		}),
		Enqueues: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_enqueues_total",
			Help: "Merge queue decisions by binding and outcome (enqueued, already_queued, error).",
		}, []string{"binding", "outcome"}),
		MergeGroupChecks: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_merge_group_checks_total",
			Help: "Review check runs posted on merge group commits by binding and conclusion (success, failure).",
		}, []string{"binding", "conclusion"}),
		GitHubErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_github_errors_total",
			Help: "Failed GitHub calls by binding and call (list_pulls, check_runs, create_check, update_check, files, rules, " +
				"comments, compare, reviews, pull_request_heads, statuses, pull_request, enqueue, merge_queue).",
		}, []string{"binding", "call"}),
		KagentErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_kagent_errors_total",
			Help: "Failed or unresolved kagent calls by binding and call (send, get_task, cancel, get_session).",
		}, []string{"binding", "call"}),
		Conditional: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_github_conditional_reads_total",
			Help: "GitHub reads sent with an ETag by result (not_modified, changed); a 304 costs no rate limit.",
		}, []string{"result"}),
		CommentWrites: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_comment_writes_total",
			Help: "Writes of the reviewer's PR comment by binding and outcome (created, updated, error). An error leaves " +
				"the check run as it is; each is also logged as \"the review comment was not written\".",
		}, []string{"binding", "outcome"}),
		ReviewSessions: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_review_sessions_total",
			Help: "Review tasks kagent accepted, by binding, path (fresh, resumed: in the session that reviewed the PR's " +
				"previous head) and reason: resumed, or why the task did not resume (none, retry, disabled, foreign, size, " +
				"unaccounted, missing, deleted, crashed, failed, revision, unreadable, refused).",
		}, []string{"binding", "path", "reason"}),
		ReviewTaskBytes: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "factory_reviewer_review_task_bytes",
			Help: "Bytes of text and data kagent keeps on a completed review's task (prompt, replies, tool calls and " +
				"results), by binding and path (fresh, resumed). Each is also logged as \"review usage\".",
			Buckets: prometheus.ExponentialBuckets(16<<10, 2, 8),
		}, []string{"binding", "path"}),
		ReviewTokens: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_review_tokens_total",
			Help: "Tokens of completed reviews' tasks that kagent reported, by binding, path (fresh, resumed) and kind " +
				"(input_uncached, input_cached, output including reasoning).",
		}, []string{"binding", "path", "kind"}),
		ReviewUsage: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_reviewer_review_usage_total",
			Help: "Completed reviews by binding, path (fresh, resumed) and usage: reported when kagent stamped the " +
				"task's token usage, else unreported (kagent v1.0.1's Codex harness reports none on the task).",
		}, []string{"binding", "path", "usage"}),
	}
	if reg != nil {
		reg.MustRegister(m.ReviewsStarted, m.ReviewsCompleted, m.Findings, m.Withdrawals, m.HumanGates, m.Retries, m.SplitRequests, m.InFlight, m.Enqueues, m.MergeGroupChecks,
			m.GitHubErrors, m.KagentErrors, m.Conditional, m.CommentWrites, m.ReviewSessions, m.ReviewTaskBytes, m.ReviewTokens, m.ReviewUsage)
	}
	return m
}
