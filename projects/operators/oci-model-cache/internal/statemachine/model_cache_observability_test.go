package statemachine

import (
	"context"
	"errors"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
)

// --- helpers ---

func newMCObs(phase string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "obs-model",
			Namespace: "default",
		},
		Status: v1alpha1.ModelCacheStatus{Phase: phase},
	}
}

func pendingState(mc *v1alpha1.ModelCache) ModelCachePending {
	return ModelCachePending{resource: mc}
}

func resolvingState(mc *v1alpha1.ModelCache) ModelCacheResolving {
	return ModelCacheResolving{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
	}
}

// --- NoOpObserver ---

// TestNoOpObserver_OnTransition verifies it does not panic.
func TestNoOpObserver_OnTransition(t *testing.T) {
	mc := newMCObs(PhasePending)
	var obs NoOpObserver
	obs.OnTransition(context.Background(), pendingState(mc), resolvingState(mc))
}

// TestNoOpObserver_OnTransitionError verifies it does not panic.
func TestNoOpObserver_OnTransitionError(t *testing.T) {
	mc := newMCObs(PhasePending)
	var obs NoOpObserver
	obs.OnTransitionError(context.Background(), pendingState(mc), resolvingState(mc), errors.New("boom"))
}

// TestNoOpObserver_ImplementsInterface verifies the compile-time interface check
// (this is a compile-time assertion that we state explicitly in tests too).
func TestNoOpObserver_ImplementsInterface(t *testing.T) {
	var _ TransitionObserver = NoOpObserver{}
}

// --- OTelObserver ---

// TestNewOTelObserver_ReturnsNonNil verifies the constructor works.
func TestNewOTelObserver_ReturnsNonNil(t *testing.T) {
	obs := NewOTelObserver("test-tracer")
	if obs == nil {
		t.Fatal("NewOTelObserver should return non-nil")
	}
}

// TestOTelObserver_OnTransition_DoesNotPanic verifies that spans are created
// without panicking (uses the no-op global tracer provider by default in tests).
func TestOTelObserver_OnTransition_DoesNotPanic(t *testing.T) {
	mc := newMCObs(PhasePending)
	obs := NewOTelObserver("test-tracer")
	obs.OnTransition(context.Background(), pendingState(mc), resolvingState(mc))
}

// TestOTelObserver_OnTransitionError_DoesNotPanic verifies error spans work.
func TestOTelObserver_OnTransitionError_DoesNotPanic(t *testing.T) {
	mc := newMCObs(PhasePending)
	obs := NewOTelObserver("test-tracer")
	obs.OnTransitionError(context.Background(), pendingState(mc), resolvingState(mc), errors.New("some error"))
}

// --- LoggingObserver ---

// TestLoggingObserver_OnTransition verifies it does not panic.
func TestLoggingObserver_OnTransition(t *testing.T) {
	mc := newMCObs(PhasePending)
	var obs LoggingObserver
	obs.OnTransition(context.Background(), pendingState(mc), resolvingState(mc))
}

// TestLoggingObserver_OnTransitionError verifies it does not panic.
func TestLoggingObserver_OnTransitionError(t *testing.T) {
	mc := newMCObs(PhasePending)
	var obs LoggingObserver
	obs.OnTransitionError(context.Background(), pendingState(mc), resolvingState(mc), errors.New("log error"))
}

// --- CompositeObserver ---

// TestCompositeObserver_Empty verifies that an empty composite observer
// does not panic.
func TestCompositeObserver_Empty(t *testing.T) {
	var comp CompositeObserver
	mc := newMCObs(PhasePending)
	comp.OnTransition(context.Background(), pendingState(mc), resolvingState(mc))
	comp.OnTransitionError(context.Background(), pendingState(mc), resolvingState(mc), errors.New("err"))
}

// TestCompositeObserver_CallsAllObservers verifies that all registered observers
// receive the OnTransition call.
func TestCompositeObserver_CallsAllObservers(t *testing.T) {
	counter1 := &recordingObserver{}
	counter2 := &recordingObserver{}

	comp := CompositeObserver{counter1, counter2}

	mc := newMCObs(PhasePending)
	comp.OnTransition(context.Background(), pendingState(mc), resolvingState(mc))

	if counter1.transitionCount != 1 {
		t.Errorf("observer1 OnTransition called %d times, want 1", counter1.transitionCount)
	}
	if counter2.transitionCount != 1 {
		t.Errorf("observer2 OnTransition called %d times, want 1", counter2.transitionCount)
	}
}

// TestCompositeObserver_CallsAllObservers_Error verifies OnTransitionError is
// broadcast to all children.
func TestCompositeObserver_CallsAllObservers_Error(t *testing.T) {
	counter1 := &recordingObserver{}
	counter2 := &recordingObserver{}

	comp := CompositeObserver{counter1, counter2}

	mc := newMCObs(PhasePending)
	comp.OnTransitionError(context.Background(), pendingState(mc), resolvingState(mc), errors.New("err"))

	if counter1.errorCount != 1 {
		t.Errorf("observer1 OnTransitionError called %d times, want 1", counter1.errorCount)
	}
	if counter2.errorCount != 1 {
		t.Errorf("observer2 OnTransitionError called %d times, want 1", counter2.errorCount)
	}
}

// TestCompositeObserver_MultipleTransitions verifies counts accumulate correctly.
func TestCompositeObserver_MultipleTransitions(t *testing.T) {
	counter := &recordingObserver{}
	comp := CompositeObserver{counter}

	mc := newMCObs(PhasePending)
	from := pendingState(mc)
	to := resolvingState(mc)

	for i := 0; i < 3; i++ {
		comp.OnTransition(context.Background(), from, to)
	}

	if counter.transitionCount != 3 {
		t.Errorf("expected 3 OnTransition calls, got %d", counter.transitionCount)
	}
}

// --- ValidateTransition ---

// TestValidateTransition_NilTarget returns nil when 'to' is nil.
func TestValidateTransition_NilTarget(t *testing.T) {
	mc := newMCObs(PhasePending)
	from := pendingState(mc)
	if err := ValidateTransition(from, nil); err != nil {
		t.Errorf("expected nil error for nil 'to' state, got %v", err)
	}
}

// TestValidateTransition_ValidState returns nil for a valid Ready state.
func TestValidateTransition_ValidState(t *testing.T) {
	mc := newMCObs(PhaseReady)
	to := ModelCacheReady{
		resource: mc,
		ResolveResult: ResolveResult{
			ResolvedRef:      "ghcr.io/r/m:t",
			Digest:           "sha256:abc",
			ResolvedRevision: "main",
			Format:           "safetensors",
		},
	}
	from := pendingState(mc)
	if err := ValidateTransition(from, to); err != nil {
		t.Errorf("expected nil error for valid Ready state, got %v", err)
	}
}

// TestValidateTransition_InvalidState returns error for invalid state.
func TestValidateTransition_InvalidState(t *testing.T) {
	mc := newMCObs(PhaseReady)
	// Ready without Digest is invalid.
	to := ModelCacheReady{
		resource: mc,
		ResolveResult: ResolveResult{
			ResolvedRef:      "ref",
			ResolvedRevision: "main",
			Format:           "gguf",
			// Digest missing
		},
	}
	from := pendingState(mc)
	if err := ValidateTransition(from, to); err == nil {
		t.Error("expected error for Ready state without Digest")
	}
}

// TestValidateTransition_PendingState is always valid.
func TestValidateTransition_PendingState(t *testing.T) {
	mc := newMCObs(PhasePending)
	from := ModelCacheUnknown{resource: mc, ObservedPhase: "something"}
	to := ModelCachePending{resource: mc}
	if err := ValidateTransition(from, to); err != nil {
		t.Errorf("Pending state should always be valid, got %v", err)
	}
}

// --- recordingObserver is a test helper ---

type recordingObserver struct {
	transitionCount int
	errorCount      int
}

func (r *recordingObserver) OnTransition(_ context.Context, _, _ ModelCacheState) {
	r.transitionCount++
}

func (r *recordingObserver) OnTransitionError(_ context.Context, _, _ ModelCacheState, _ error) {
	r.errorCount++
}
