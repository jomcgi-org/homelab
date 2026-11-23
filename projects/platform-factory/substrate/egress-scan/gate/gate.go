package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"
)

// gate is the HTTP endpoint that egress-scan calls. It takes one body, sends it
// to one child that it created itself, and returns the engine reply unchanged.
//
// Isolation rules:
//   - The caller names nothing. The handler reads the body and one optional
//     deadline header. No header, query or path of the caller reaches the
//     router or the ate API, and the child is chosen from the gate's own pool.
//   - The gate never parses the body and never logs it. It does not parse the
//     reply: a bounded copy goes back to the caller.
type gate struct {
	cfg    config
	pool   *pool
	router router
	m      *metrics
	log    *limitedLog
	slots  chan struct{}
	now    func() time.Time
}

func newGate(cfg config, p *pool, r router, m *metrics, log *limitedLog) *gate {
	return &gate{cfg: cfg, pool: p, router: r, m: m, log: log, slots: make(chan struct{}, cfg.MaxInFlight), now: time.Now}
}

func (g *gate) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /scan", g.serveScan)
	return mux
}

// fail writes a plain failure. The body names no internal detail.
func (g *gate) fail(w http.ResponseWriter, outcome string, code int, began time.Time) {
	g.m.requests.WithLabelValues(outcome).Inc()
	g.m.requestSeconds.Observe(time.Since(began).Seconds())
	http.Error(w, outcome, code)
}

func (g *gate) serveScan(w http.ResponseWriter, r *http.Request) {
	began := time.Now()
	deadline, ok := g.deadline(r)
	if !ok {
		g.fail(w, outcomeBadRequest, http.StatusBadRequest, began)
		return
	}
	// The size limit applies before the gate reads a byte, and before the
	// request takes a scan slot.
	if r.ContentLength > g.cfg.MaxBody {
		g.fail(w, outcomeOversize, http.StatusRequestEntityTooLarge, began)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), deadline)
	defer cancel()

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, g.cfg.MaxBody))
	var tooBig *http.MaxBytesError
	switch {
	case errors.As(err, &tooBig):
		g.fail(w, outcomeOversize, http.StatusRequestEntityTooLarge, began)
		return
	case err != nil || len(body) == 0:
		g.fail(w, outcomeBadRequest, http.StatusBadRequest, began)
		return
	}

	if !g.admit(ctx) {
		if r.Context().Err() != nil {
			g.fail(w, outcomeClientGone, http.StatusServiceUnavailable, began)
		} else {
			g.fail(w, outcomeShed, http.StatusServiceUnavailable, began)
		}
		return
	}
	g.m.children.WithLabelValues("inflight").Set(float64(len(g.slots)))
	defer func() {
		<-g.slots
		g.m.children.WithLabelValues("inflight").Set(float64(len(g.slots)))
	}()

	reply, outcome := g.scan(ctx, r.Context(), body)
	if outcome != outcomeOK {
		code := http.StatusBadGateway
		switch outcome {
		case outcomePoolEmpty, outcomeNoWorker, outcomeRouterDown:
			code = http.StatusServiceUnavailable
		case outcomeDeadline:
			code = http.StatusGatewayTimeout
		}
		g.fail(w, outcome, code, began)
		g.log.warn("outcome:"+outcome, "scan failed", "outcome", outcome, "body_bytes", len(body), "elapsed_ms", time.Since(began).Milliseconds())
		return
	}
	g.m.requests.WithLabelValues(outcomeOK).Inc()
	g.m.requestSeconds.Observe(time.Since(began).Seconds())
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(reply)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(reply)
}

// deadline returns the time budget of a request: the caller's X-Deadline-Ms,
// at most MaxDeadline, or DefaultDeadline. A malformed value is a bad request.
func (g *gate) deadline(r *http.Request) (time.Duration, bool) {
	v := r.Header.Get("X-Deadline-Ms")
	if v == "" {
		return g.cfg.DefaultDeadline, true
	}
	ms, err := strconv.ParseInt(v, 10, 64)
	if err != nil || ms < 1 {
		return 0, false
	}
	// Clamp in milliseconds before the multiplication: a huge value would
	// overflow time.Duration and wrap negative.
	if limit := g.cfg.MaxDeadline.Milliseconds(); ms > limit {
		ms = limit
	}
	return time.Duration(ms) * time.Millisecond, true
}

// admit takes a scan slot. A full gate queues the request for QueueWait, then
// sheds it.
func (g *gate) admit(ctx context.Context) bool {
	select {
	case g.slots <- struct{}{}:
		return true
	default:
	}
	t := time.NewTimer(g.cfg.QueueWait)
	defer t.Stop()
	select {
	case g.slots <- struct{}{}:
		return true
	case <-t.C:
	case <-ctx.Done():
	}
	return false
}

// scan sends the body to a child, and to a second one if the first is dead.
// ctx is the request deadline; callerCtx ends when the scanner disconnects.
func (g *gate) scan(ctx, callerCtx context.Context, body []byte) ([]byte, string) {
	var last *sendError
	for attempt := 0; attempt < 2; attempt++ {
		c, outcome := g.take(ctx)
		if c == nil {
			if outcome == outcomePoolEmpty && attempt > 0 && last != nil {
				// The first child failed, and no other can take the body in time.
				return nil, outcomeChildError
			}
			return nil, outcome
		}
		// The scanner may have left while the gate took the child. The body has
		// not left yet, so the child is unspent.
		if ctx.Err() != nil {
			g.pool.release(c)
			return nil, g.ended(ctx, callerCtx)
		}
		res, err := g.router.Send(ctx, g.pool.cfg.Atespace+"/"+c.name, body)
		if err == nil {
			g.m.connectSeconds.Observe(res.Connect.Seconds())
			g.m.engineSeconds.Observe(res.Engine.Seconds())
			return res.Body, outcomeOK
		}
		var se *sendError
		if !errors.As(err, &se) {
			se = &sendError{Kind: sendNoReply}
		}
		last = se
		switch se.Kind {
		case sendUnsent:
			// The router is out of reach. The body never left, so the child is
			// still good.
			g.pool.release(c)
			return nil, outcomeRouterDown
		case sendTimeout:
			// The child may have read the body. It is spent.
			g.pool.retire(c)
			return nil, g.ended(ctx, callerCtx)
		case sendTooLarge:
			g.pool.retire(c)
			return nil, outcomeBadReply
		}
		g.pool.retire(c)
		if !se.retryable() || attempt == 1 || remaining(ctx) < g.cfg.RetryMinRemaining {
			return nil, outcomeChildError
		}
		g.m.retries.Inc()
	}
	return nil, outcomeChildError
}

// take claims a waiting child, or creates one when none waits (path A).
func (g *gate) take(ctx context.Context) (*child, string) {
	if c, ok := g.pool.claim(); ok {
		return c, ""
	}
	g.m.poolMiss.Inc()
	c, err := g.pool.fallbackChild(ctx)
	switch {
	case err == nil:
		return c, ""
	case errors.Is(err, errNoWorker):
		return nil, outcomeNoWorker
	case errors.Is(err, errPoolEmpty):
		return nil, outcomePoolEmpty
	case ctx.Err() != nil:
		return nil, outcomeDeadline
	default:
		return nil, outcomeNoWorker
	}
}

// ended names why a request context ended.
func (g *gate) ended(ctx, callerCtx context.Context) string {
	if callerCtx.Err() != nil {
		return outcomeClientGone
	}
	return outcomeDeadline
}

func remaining(ctx context.Context) time.Duration {
	if dl, ok := ctx.Deadline(); ok {
		return time.Until(dl)
	}
	return time.Hour
}

// adminHandler serves the probes and the metrics on a separate port, so the
// network policy can admit only the scanner on the scan port.
func adminHandler(p *pool, m *metrics, metricsHandler http.Handler) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok\n") })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !p.ready() {
			http.Error(w, "not ready", http.StatusServiceUnavailable)
			return
		}
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.Handle("GET /metrics", metricsHandler)
	return mux
}
