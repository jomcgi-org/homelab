package detect

import (
	"context"
	"errors"
	"fmt"
)

// Outcome is what the detector found in a request.
type Outcome string

const (
	// OutcomeClean means no ClassBlock finding and every call gave a verdict.
	OutcomeClean Outcome = "clean"
	// OutcomeBlocked means a ClassBlock finding.
	OutcomeBlocked Outcome = "blocked"
	// OutcomeFailed means a call gave no verdict and no finding came before it.
	OutcomeFailed Outcome = "failed"
)

// Failure says why a call gave no verdict.
type Failure string

const (
	FailureNone     Failure = ""
	FailureTimeout  Failure = "timeout"
	FailureBudget   Failure = "budget"
	FailureError    Failure = "error"
	FailureOversize Failure = "oversize"
	FailurePanic    Failure = "panic"
)

// Verdict is the host's answer for one request.
type Verdict struct {
	Outcome Outcome
	// Findings holds every finding of the calls that ran, ClassReport included.
	Findings []Finding
	Failure  Failure
	// Deny is the decision for the request: true for a blocked request in
	// enforce mode, and for a failed one in enforce mode with failure mode deny.
	Deny bool
	// Canceled is set when the caller's context ended. It is no verdict: the
	// caller returns its own error.
	Canceled error
}

// Host runs one detector under the limits, mode and failure mode of its Config.
type Host struct {
	det Detector
	cfg Config
}

// NewHost wraps a detector. Use New to build one from the registry.
func NewHost(d Detector, cfg Config) *Host { return &Host{det: d, cfg: cfg} }

// Name returns the detector's name.
func (h *Host) Name() string { return h.det.Name() }

// Config returns the configuration of the host.
func (h *Host) Config() Config { return h.cfg }

// Health returns nil for a detector with no private data, and the data health
// of a PrivateData detector.
func (h *Host) Health() error {
	if p, ok := h.det.(PrivateData); ok {
		return p.Health()
	}
	return nil
}

// Reload reloads the private data of a PrivateData detector. It does nothing for
// another detector.
func (h *Host) Reload() error {
	if p, ok := h.det.(PrivateData); ok {
		return p.Reload()
	}
	return nil
}

// Scan runs the detector on each input until one has a ClassBlock finding or a
// call fails. All calls share Limits.Deadline. An input over Limits.MaxInputBytes
// is a failure, never a skip.
func (h *Host) Scan(ctx context.Context, inputs ...Input) Verdict {
	callCtx := ctx
	if d := h.cfg.Limits.Deadline; d > 0 {
		var cancel context.CancelFunc
		callCtx, cancel = context.WithTimeout(ctx, d)
		defer cancel()
	}
	v := Verdict{Outcome: OutcomeClean}
	for _, in := range inputs {
		if len(in.Bytes) == 0 {
			continue
		}
		if max := h.cfg.Limits.MaxInputBytes; max > 0 && len(in.Bytes) > max {
			return h.failed(v, FailureOversize)
		}
		found, failure := h.call(ctx, callCtx, in)
		if ctx.Err() != nil {
			v.Canceled = ctx.Err()
			return v
		}
		v.Findings = append(v.Findings, found...)
		if failure != FailureNone {
			return h.failed(v, failure)
		}
		for _, f := range found {
			if f.Class == ClassBlock {
				v.Outcome = OutcomeBlocked
				v.Deny = h.cfg.Mode == ModeEnforce
				return v
			}
		}
	}
	return v
}

func (h *Host) failed(v Verdict, failure Failure) Verdict {
	v.Outcome = OutcomeFailed
	v.Failure = failure
	v.Deny = h.cfg.Mode == ModeEnforce && h.cfg.FailureMode == FailDeny
	return v
}

type callResult struct {
	found   []Finding
	failure Failure
}

// call runs the detector in its own goroutine, so that a detector that ignores
// its context cannot hold the request past the deadline, and a panic is a
// failure. The goroutine ends when the detector returns.
func (h *Host) call(parent, ctx context.Context, in Input) ([]Finding, Failure) {
	done := make(chan callResult, 1)
	go func() {
		defer func() {
			if recover() != nil {
				done <- callResult{failure: FailurePanic}
			}
		}()
		found, err := h.det.Detect(ctx, in)
		done <- h.result(parent, found, err)
	}()
	select {
	case r := <-done:
		return r.found, r.failure
	case <-ctx.Done():
		return nil, FailureTimeout
	}
}

func (h *Host) result(parent context.Context, found []Finding, err error) callResult {
	name := h.det.Name()
	for i := range found {
		if found[i].Detector == "" {
			found[i].Detector = name
		}
		if found[i].Class == "" {
			found[i].Class = ClassBlock
		}
	}
	switch {
	case err == nil:
		return callResult{found: found}
	case len(found) > 0 && !errors.Is(err, context.Canceled):
		// A finding before the error is a verdict: the request is blocked.
		return callResult{found: found}
	case errors.Is(err, ErrBudget):
		return callResult{failure: FailureBudget}
	case errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled):
		return callResult{failure: FailureTimeout}
	default:
		return callResult{failure: FailureError}
	}
}

// String names the verdict for logs. It holds no finding detail.
func (v Verdict) String() string {
	return fmt.Sprintf("%s failure=%q deny=%t findings=%d", v.Outcome, v.Failure, v.Deny, len(v.Findings))
}
