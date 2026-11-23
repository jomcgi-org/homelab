package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"
)

// Observe mode runs the engine off the request path. The reply comes from
// Gitleaks alone. The engine verdict is only counted and logged, so that an
// operator can see false positives and engine errors before the tier denies.
//
// Bounds. Observe admits an engine call before it starts a goroutine. It uses
// the same in-flight limit, breaker and deadline as enforce mode. So the
// number of observe goroutines never exceeds semgrep.concurrency, and each
// ends at the deadline. The call uses a context with no link to the request,
// so the reply never waits for it and the end of the request does not cancel
// it. A request that the limit, the breaker or the size limit refuses starts
// no goroutine and copies no body.

// Observe results. They describe what enforce mode would have changed for a
// request that Gitleaks allowed.
const (
	observeWouldDeny = "would_deny"
	observeWouldFail = "would_fail"
	observeNoChange  = "no_change"
)

var observeResults = []string{observeWouldDeny, observeWouldFail, observeNoChange}

func (t *semgrepTier) observing() bool { return t.cfg.Mode == modeObserve }

// Gitleaks verdict states in an observation.
const (
	glUnknown = iota
	glClean
	glFound
	glAborted
)

// observation joins the Gitleaks verdict and the engine result of one request.
// Whichever side comes second records the pair. No side waits for the other,
// so a missing side can never hold a goroutine.
type observation struct {
	tier *semgrepTier

	mu       sync.Mutex
	gitleaks int
	engine   *engineResult
	recorded bool
}

func (o *observation) setGitleaks(state int) {
	o.mu.Lock()
	o.gitleaks = state
	o.mu.Unlock()
	o.settle()
}

func (o *observation) setEngine(res engineResult) {
	o.mu.Lock()
	o.engine = &res
	o.mu.Unlock()
	o.settle()
}

func (o *observation) settle() {
	o.mu.Lock()
	if o.recorded || o.gitleaks == glUnknown || o.engine == nil {
		o.mu.Unlock()
		return
	}
	o.recorded = true
	state, res := o.gitleaks, *o.engine
	o.mu.Unlock()
	if state == glAborted {
		// Gitleaks gave no verdict (work limit or cancelled call).
		return
	}
	o.tier.metrics.record(state == glFound, res)
}

// beginObserve admits one engine call and starts it in the background. It
// returns at once. The returned observation always gets the engine result.
//
// The engine text is built here, before the goroutine starts, and only when the
// call is admitted. The goroutine holds that copy and the immutable strings
// only. It never reads the request body after the handler returns, so it does
// not depend on how the gRPC codec reuses its buffers.
func (t *semgrepTier) beginObserve(fields string, body []byte, decoded string) *observation {
	obs := &observation{tier: t}
	textLen := engineTextLen(fields, body, decoded)
	t.metrics.observeSizes(len(body), textLen)
	began := time.Now()
	if refused := t.admit(len(body), textLen); refused != "" {
		t.finishObserved(obs, engineResult{outcome: refused}, time.Since(began))
		return obs
	}
	payload := engineText(fields, body, decoded)
	go func() {
		defer t.releaseSlot()
		var res engineResult
		defer func() {
			// A panic outside the engine call. Settle the breaker probe, and
			// count the call. The panic value is dropped.
			if recover() != nil {
				t.breaker.abandon()
				res = engineResult{outcome: engineError, err: errEnginePanic}
			}
			t.finishObserved(obs, res, time.Since(began))
		}()
		// No link to the request context: the reply never waits for this call.
		res = t.exec(context.Background(), payload)
	}()
	return obs
}

func (t *semgrepTier) finishObserved(obs *observation, res engineResult, elapsed time.Duration) {
	t.metrics.observe(res.outcome, elapsed)
	switch {
	case res.outcome == engineFound:
		t.logObservedFound(res.verdict)
	case res.failed():
		t.logObservedFailure(res)
	}
	obs.setEngine(res)
}

// logObservedFound writes rule ids and positions. It never writes the matched
// text. The line is rate limited like the enforce log line.
func (t *semgrepTier) logObservedFound(v Verdict) {
	n, ok := t.allowLog("observe_" + engineFound)
	if !ok {
		return
	}
	parts := make([]string, 0, maxLoggedFindings)
	for i, f := range v.Findings {
		if i == maxLoggedFindings {
			break
		}
		parts = append(parts, fmt.Sprintf("%s@%d-%d", f.RuleID, f.Start, f.End))
	}
	log.Printf("semgrep tier observed a finding, request not blocked: %d finding(s): %s%s", len(v.Findings), strings.Join(parts, " "), suppressedNote(n))
}

func (t *semgrepTier) logObservedFailure(res engineResult) {
	n, ok := t.allowLog("observe_" + res.outcome)
	if !ok {
		return
	}
	detail := ""
	if res.err != nil && res.outcome == engineError {
		detail = ": " + errorKind(res.err)
	}
	log.Printf("semgrep tier observed %s, request not blocked%s%s", res.outcome, detail, suppressedNote(n))
}
