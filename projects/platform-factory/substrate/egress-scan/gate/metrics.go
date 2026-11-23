package main

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
)

// Request outcomes. The scanner sees 200 for outcomeOK and a failure status for
// the others, and applies its own failure mode.
const (
	outcomeOK         = "ok"
	outcomeBadRequest = "bad_request"
	outcomeOversize   = "oversize"
	outcomeShed       = "shed"
	outcomePoolEmpty  = "pool_empty"
	outcomeNoWorker   = "no_worker"
	outcomeDeadline   = "deadline"
	outcomeClientGone = "client_gone"
	outcomeChildError = "child_error"
	outcomeRouterDown = "router_error"
	outcomeBadReply   = "bad_response"
)

var outcomes = []string{
	outcomeOK, outcomeBadRequest, outcomeOversize, outcomeShed, outcomePoolEmpty, outcomeNoWorker,
	outcomeDeadline, outcomeClientGone, outcomeChildError, outcomeRouterDown, outcomeBadReply,
}

// metrics holds every series of the gate. None has a label whose value comes
// from a request: outcomes, states and operations are fixed sets.
type metrics struct {
	reg *prometheus.Registry

	children  *prometheus.GaugeVec
	requests  *prometheus.CounterVec
	poolMiss  prometheus.Counter
	fallbacks *prometheus.CounterVec
	replenish *prometheus.CounterVec
	recycled  prometheus.Counter
	childErrs prometheus.Counter
	retries   prometheus.Counter
	spent     prometheus.Gauge

	requestSeconds prometheus.Histogram
	claimSeconds   prometheus.Histogram
	createSeconds  prometheus.Histogram
	resumeSeconds  prometheus.Histogram
	connectSeconds prometheus.Histogram
	engineSeconds  prometheus.Histogram
}

func histogram(name, help string) prometheus.Histogram {
	return prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "factory_engine_gate_" + name,
		Help:    help,
		Buckets: []float64{.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
	})
}

func newMetrics() *metrics {
	m := &metrics{
		reg: prometheus.NewRegistry(),
		children: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "factory_engine_gate_children",
			Help: "Children by state: waiting (claimable), creating, inflight (scanning).",
		}, []string{"state"}),
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_engine_gate_requests_total", Help: "Scan requests by outcome.",
		}, []string{"outcome"}),
		poolMiss: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_engine_gate_pool_misses_total", Help: "Requests that found no waiting child.",
		}),
		fallbacks: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_engine_gate_fallbacks_total", Help: "Path A attempts (create and resume on the request path) by result.",
		}, []string{"result"}),
		replenish: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "factory_engine_gate_replenish_failures_total", Help: "Failed ate API operations of the pool by operation.",
		}, []string{"op"}),
		recycled: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_engine_gate_recycled_total", Help: "Waiting children replaced before their maximum lifetime.",
		}),
		childErrs: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_engine_gate_child_errors_total", Help: "Children that failed a request or died while they waited.",
		}),
		retries: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "factory_engine_gate_retries_total", Help: "Requests sent again to the next child.",
		}),
		spent: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "factory_engine_gate_spent_children", Help: "Served children that the control plane has not deleted yet (last sweep).",
		}),
		requestSeconds: histogram("request_seconds", "Time from the scanner request to the reply."),
		claimSeconds:   histogram("claim_seconds", "Time to take a waiting child, or to find none."),
		createSeconds:  histogram("create_seconds", "CreateActor latency."),
		resumeSeconds:  histogram("resume_seconds", "ResumeActor latency (the fork)."),
		connectSeconds: histogram("router_connect_seconds", "New connection to the router: TCP and TLS."),
		engineSeconds:  histogram("send_to_result_seconds", "Request written to response read, through the router and the child."),
	}
	m.reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
		m.children, m.requests, m.poolMiss, m.fallbacks, m.replenish, m.recycled, m.childErrs, m.retries, m.spent,
		m.requestSeconds, m.claimSeconds, m.createSeconds, m.resumeSeconds, m.connectSeconds, m.engineSeconds)
	for _, o := range outcomes {
		m.requests.WithLabelValues(o)
	}
	for _, s := range []string{"waiting", "creating", "inflight"} {
		m.children.WithLabelValues(s)
	}
	for _, r := range []string{"ok", "failed", "refused", "skipped"} {
		m.fallbacks.WithLabelValues(r)
	}
	for _, op := range []string{"list", "create", "resume", "delete"} {
		m.replenish.WithLabelValues(op)
	}
	return m
}
