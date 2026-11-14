package statemachine

import (
	"context"
	"fmt"
	"math"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
)

func newMCForMetrics(name, namespace, phase string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Status: v1alpha1.ModelCacheStatus{
			Phase: phase,
		},
	}
}

// TestNewMetricsObserver verifies the constructor returns a non-nil observer.
func TestNewMetricsObserver(t *testing.T) {
	obs := NewMetricsObserver()
	if obs == nil {
		t.Fatal("NewMetricsObserver() should return non-nil")
	}
	if obs.transitionStart == nil {
		t.Error("transitionStart map should be initialized")
	}
}

// TestMetricsObserver_OnTransition_FirstTime verifies OnTransition does not
// panic when called for a resource that has no prior transition start time.
func TestMetricsObserver_OnTransition_FirstTime(t *testing.T) {
	obs := NewMetricsObserver()

	mc := newMCForMetrics("model1", "default", PhasePending)
	from := ModelCachePending{resource: mc}
	to := ModelCacheResolving{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
	}

	// Should not panic.
	obs.OnTransition(context.Background(), from, to)

	// The start time for the resource should have been recorded.
	key := mc.Namespace + "/" + mc.Name
	obs.mu.Lock()
	_, ok := obs.transitionStart[key]
	obs.mu.Unlock()
	if !ok {
		t.Error("transitionStart should be recorded after first transition")
	}
}

// TestMetricsObserver_OnTransition_SubsequentTransition verifies that calling
// OnTransition a second time observes the duration from the previous transition
// and updates the start time.
func TestMetricsObserver_OnTransition_SubsequentTransition(t *testing.T) {
	obs := NewMetricsObserver()

	mc := newMCForMetrics("model2", "default", PhasePending)
	key := mc.Namespace + "/" + mc.Name

	// Pre-seed a start time in the past.
	past := time.Now().Add(-5 * time.Second)
	obs.mu.Lock()
	obs.transitionStart[key] = past
	obs.mu.Unlock()

	from := ModelCachePending{resource: mc}
	to := ModelCacheResolving{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
	}

	// Should not panic; also records the histogram.
	obs.OnTransition(context.Background(), from, to)

	// Start time should be updated to now (after the call).
	obs.mu.Lock()
	newStart := obs.transitionStart[key]
	obs.mu.Unlock()
	if !newStart.After(past) {
		t.Error("transitionStart should be updated after subsequent transition")
	}
}

// These tests share the production collectors. Keep them serial and compare
// snapshots so previous tests and repeated runs can accumulate observations.
func metricsRegistry(t *testing.T) *prometheus.Registry {
	t.Helper()
	registry := prometheus.NewRegistry()
	for _, collector := range []prometheus.Collector{reconcileTotal, reconcileDuration, errorsTotal, resourcePhase} {
		if err := registry.Register(collector); err != nil {
			t.Fatalf("register collector: %v", err)
		}
	}
	return registry
}

func gatherMetrics(t *testing.T, registry *prometheus.Registry) []*dto.MetricFamily {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}
	return families
}

// findMetric preserves the distinction between absent and present zero samples.
func findMetric(families []*dto.MetricFamily, name string, labels map[string]string) (*dto.Metric, bool) {
	for _, family := range families {
		if family.GetName() != name {
			continue
		}
		for _, metric := range family.GetMetric() {
			if len(metric.GetLabel()) != len(labels) {
				continue
			}
			matches := true
			for _, label := range metric.GetLabel() {
				value, ok := labels[label.GetName()]
				if !ok || value != label.GetValue() {
					matches = false
					break
				}
			}
			if matches {
				return metric, true
			}
		}
	}
	return nil, false
}

func assertCounterDeltas(t *testing.T, before, after []*dto.MetricFamily, family, label string, values []string, selected string) {
	t.Helper()
	for _, value := range values {
		labels := map[string]string{label: value}
		previous, _ := findMetric(before, family, labels)
		current, present := findMetric(after, family, labels)
		want := 0.0
		if value == selected {
			want = 1
			if !present {
				t.Errorf("%s{%s=%q} absent after increment", family, label, value)
			}
		}
		if delta := current.GetCounter().GetValue() - previous.GetCounter().GetValue(); delta != want {
			t.Errorf("%s{%s=%q} counter delta = %g, want %g", family, label, value, delta, want)
		}
	}
}

var metricErrorTypes = []string{"transient", "permanent", "transition_error", "unknown_type"}

func TestMetricsObserver_OnTransitionError(t *testing.T) {
	registry := metricsRegistry(t)
	obs := NewMetricsObserver()
	mc := newMCForMetrics("model3", "default", PhasePending)
	from := ModelCachePending{resource: mc}
	to := ModelCacheResolving{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
	}
	before := gatherMetrics(t, registry)
	obs.OnTransitionError(context.Background(), from, to, errValidation)
	assertCounterDeltas(t, before, gatherMetrics(t, registry), "modelcache_errors_total", "error_type", metricErrorTypes, "transition_error")
}

func TestRecordReconcile_Metrics(t *testing.T) {
	registry := metricsRegistry(t)
	for _, phase := range []string{PhasePending, PhaseResolving, PhaseSyncing, PhaseReady, PhaseFailed} {
		for _, success := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/success=%t", phase, success), func(t *testing.T) {
				duration := 50 * time.Millisecond
				before := gatherMetrics(t, registry)
				RecordReconcile(phase, duration, success)
				after := gatherMetrics(t, registry)
				result := "error"
				if success {
					result = "success"
				}
				assertCounterDeltas(t, before, after, "modelcache_reconcile_total", "result", []string{"success", "error"}, result)
				labels := map[string]string{"phase": phase}
				previous, _ := findMetric(before, "modelcache_reconcile_duration_seconds", labels)
				current, present := findMetric(after, "modelcache_reconcile_duration_seconds", labels)
				if !present {
					t.Fatal("reconciliation duration histogram absent after observation")
				}
				if delta := current.GetHistogram().GetSampleCount() - previous.GetHistogram().GetSampleCount(); delta != 1 {
					t.Errorf("reconciliation duration count delta = %d, want 1", delta)
				}
				if delta := current.GetHistogram().GetSampleSum() - previous.GetHistogram().GetSampleSum(); math.Abs(delta-duration.Seconds()) > 1e-9 {
					t.Errorf("reconciliation duration sum delta = %g, want %g seconds", delta, duration.Seconds())
				}
			})
		}
	}
}

func TestRecordError_Metrics(t *testing.T) {
	registry := metricsRegistry(t)
	for _, errorType := range metricErrorTypes {
		t.Run(errorType, func(t *testing.T) {
			before := gatherMetrics(t, registry)
			RecordError(errorType)
			assertCounterDeltas(t, before, gatherMetrics(t, registry), "modelcache_errors_total", "error_type", metricErrorTypes, errorType)
		})
	}
}

// TestCleanupResourceMetrics_DoesNotPanic verifies CleanupResourceMetrics does
// not panic for a resource that has no registered metrics.
func TestCleanupResourceMetrics_DoesNotPanic(t *testing.T) {
	// No metrics for this resource — the function should handle that gracefully.
	CleanupResourceMetrics("non-existent-ns", "non-existent-name")
}

func TestCleanupResourceMetrics_AfterPhaseGauge(t *testing.T) {
	registry := metricsRegistry(t)
	phases := []string{PhasePending, PhaseResolving, PhaseSyncing, PhaseReady, PhaseFailed, "Unknown"}
	resources := []struct{ namespace, name string }{
		{"cleanup-ns", "cleanup-model"},
		{"cleanup-ns", "control-model"},
		{"control-ns", "cleanup-model"},
	}
	for _, resource := range resources {
		for i, phase := range phases {
			// Include a present zero sample, which must still be deleted.
			resourcePhase.WithLabelValues(resource.namespace, resource.name, phase).Set(float64(i))
			t.Cleanup(func() {
				resourcePhase.DeleteLabelValues(resource.namespace, resource.name, phase)
			})
		}
	}
	assertGauges := func(families []*dto.MetricFamily, cleaned bool) {
		t.Helper()
		for index, resource := range resources {
			for i, phase := range phases {
				labels := map[string]string{"namespace": resource.namespace, "name": resource.name, "phase": phase}
				metric, present := findMetric(families, "modelcache_resource_phase", labels)
				if cleaned && index == 0 {
					if present {
						t.Errorf("target phase %s still present after cleanup", phase)
					}
				} else if !present {
					t.Errorf("phase gauge %v absent, want present with value %d", labels, i)
				} else if got := metric.GetGauge().GetValue(); got != float64(i) {
					t.Errorf("phase gauge %v = %g, want %d", labels, got, i)
				}
			}
		}
	}
	assertGauges(gatherMetrics(t, registry), false)
	CleanupResourceMetrics(resources[0].namespace, resources[0].name)
	assertGauges(gatherMetrics(t, registry), true)
}

// TestMetricsObserver_ConcurrentTransitions verifies that concurrent calls to
// OnTransition from different goroutines do not cause data races.
func TestMetricsObserver_ConcurrentTransitions(t *testing.T) {
	obs := NewMetricsObserver()

	done := make(chan struct{})
	for i := 0; i < 5; i++ {
		go func(idx int) {
			defer func() { done <- struct{}{} }()
			mc := newMCForMetrics("concurrent-model", "default", PhasePending)
			from := ModelCachePending{resource: mc}
			to := ModelCacheResolving{
				resource:      mc,
				ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
			}
			obs.OnTransition(context.Background(), from, to)
		}(i)
	}
	for i := 0; i < 5; i++ {
		<-done
	}
}

// errValidation is a simple error value for testing.
var errValidation = fmt.Errorf("validation error")
