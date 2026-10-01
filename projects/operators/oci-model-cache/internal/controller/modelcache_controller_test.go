package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/go-logr/logr"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	sm "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/statemachine"
)

// --- Test helpers ---

// controllerScheme returns a scheme with all required types registered.
func controllerScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	require.NoError(t, v1alpha1.AddToScheme(scheme))
	require.NoError(t, batchv1.AddToScheme(scheme))
	require.NoError(t, corev1.AddToScheme(scheme))
	return scheme
}

// newReconcilerWith creates a ModelCacheReconciler backed by a fake client.
func newReconcilerWith(t *testing.T, resolver Resolver, objects ...runtime.Object) *ModelCacheReconciler {
	t.Helper()
	scheme := controllerScheme(t)
	builder := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(&v1alpha1.ModelCache{})
	for _, obj := range objects {
		builder = builder.WithRuntimeObjects(obj)
	}
	c := builder.Build()
	return &ModelCacheReconciler{
		Client:     c,
		Scheme:     scheme,
		Resolver:   resolver,
		Calculator: sm.NewModelCacheCalculator(logr.Discard()),
		Observer:   sm.NoOpObserver{},
		Config: config.Config{
			Namespace: "oci-model-cache",
			CopyImage: "ghcr.io/jomcgi/homelab/bazel/tools/hf2oci:main",
		},
	}
}

// mcRequest returns a ctrl.Request for a ModelCache in the "default" namespace.
func mcRequest(name string) ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Name: name, Namespace: "default"}}
}

// makeTestMC creates a minimal ModelCache for controller tests.
func makeTestMC(name, phase string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
		},
		Spec: v1alpha1.ModelCacheSpec{
			Repo:     "bartowski/llama",
			Registry: "ghcr.io/jomcgi/models",
			Revision: "main",
		},
		Status: v1alpha1.ModelCacheStatus{
			Phase: phase,
		},
	}
}

// fakeResolver is a mock Resolver for testing.
type fakeResolver struct {
	result *ResolveResult
	err    error
}

func (f *fakeResolver) Resolve(_ context.Context, _, _, _, _ string) (*ResolveResult, error) {
	return f.result, f.err
}

// --- Tests for Reconcile ---

// TestReconcile_NotFound verifies that a missing resource returns no error.
func TestReconcile_NotFound(t *testing.T) {
	r := newReconcilerWith(t, &fakeResolver{})
	req := mcRequest("missing")

	result, err := r.Reconcile(context.Background(), req)

	require.NoError(t, err)
	assert.Equal(t, ctrl.Result{}, result)
}

// TestReconcile_PendingTransientError verifies transient errors cause requeue
// without a status update (no SSA patch needed).
func TestReconcile_PendingTransientError(t *testing.T) {
	mc := makeTestMC("llama", sm.PhasePending)

	resolver := &fakeResolver{
		err: errors.New("temporary network failure"),
	}

	r := newReconcilerWith(t, resolver, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0), "transient error should cause requeue")
}

// TestReconcile_PendingPermanentError verifies permanent errors use
// Status().Update() which is supported by fake client.
func TestReconcile_PendingPermanentError(t *testing.T) {
	mc := makeTestMC("llama", sm.PhasePending)

	resolver := &fakeResolver{
		err: &PermanentError{Err: errors.New("repo does not exist")},
	}

	r := newReconcilerWith(t, resolver, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0), "permanent error should requeue after delay")
}

// TestReconcile_Ready_NoSpecChange verifies Ready stays Ready when spec is unchanged.
// Ready with no spec change just returns RequeueAfter — no status update needed.
func TestReconcile_Ready_NoSpecChange(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseReady)
	mc.Generation = 1
	mc.Status.ObservedGeneration = 1
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
	mc.Status.Digest = "sha256:abc"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	// Ready state should requeue after a long interval.
	assert.Greater(t, result.RequeueAfter, time.Duration(0))
}

// TestReconcile_Failed_Permanent verifies that permanent failures stay in Failed.
// This path calls RetryBackoff() without an SSA patch.
func TestReconcile_Failed_Permanent(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseFailed)
	mc.Generation = 1
	mc.Status.ObservedGeneration = 1
	mc.Status.ErrorMessage = "repo not found"
	mc.Status.LastState = sm.PhasePending
	mc.Status.Permanent = true

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0), "permanent failure should still requeue slowly")
}

// TestReconcile_Syncing_JobRunning verifies Syncing stays in Syncing when job runs.
// Running job path returns RequeueAfter without SSA patch.
func TestReconcile_Syncing_JobRunning(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseSyncing)
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"
	mc.Status.SyncJobName = "sync-job-abc"

	// Create a running job (no conditions set).
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sync-job-abc",
			Namespace: "oci-model-cache",
		},
		Status: batchv1.JobStatus{},
	}

	r := newReconcilerWith(t, &fakeResolver{}, mc, job)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0), "syncing with running job should requeue")
}

// TestReconcile_Failed_SpecChanged_ResetsToP ending verifies that a spec change
// in Failed state resets it via Status().Update() (not SSA).
func TestReconcile_Failed_SpecChanged_ResetsToPending(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseFailed)
	mc.Generation = 2                // spec changed
	mc.Status.ObservedGeneration = 1 // previous generation
	mc.Status.ErrorMessage = "old error"
	mc.Status.LastState = sm.PhasePending
	mc.Status.Permanent = true // even permanent failures reset on spec change

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.True(t, result.Requeue, "spec change in Failed should trigger immediate requeue")
}

// TestPodToModelCacheRequests_WithAnnotation verifies mapping from pod to request.
func TestPodToModelCacheRequests_WithAnnotation(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
			Annotations: map[string]string{
				"oci-model-cache.jomcgi.dev/waiting-for": "my-model",
			},
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	require.Len(t, requests, 1)
	assert.Equal(t, "my-model", requests[0].Name)
}

// TestPodToModelCacheRequests_NoAnnotation verifies empty result without annotation.
func TestPodToModelCacheRequests_NoAnnotation(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	assert.Empty(t, requests)
}

// TestPodToModelCacheRequests_NonPodObject verifies non-pod objects return nil.
func TestPodToModelCacheRequests_NonPodObject(t *testing.T) {
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: "some-job"},
	}

	requests := podToModelCacheRequests(context.Background(), job)
	assert.Nil(t, requests)
}

// TestReconcile_EmptyPhase verifies that an empty phase (initial state) works.
// This uses the transient error path (no SSA).
func TestReconcile_EmptyPhase_TransientResolver(t *testing.T) {
	mc := makeTestMC("llama", "")

	resolver := &fakeResolver{
		err: errors.New("transient error"),
	}

	r := newReconcilerWith(t, resolver, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0))
}

// TestReconcile_InvalidPhase_ResetsToPending verifies that an unrecognized
// phase triggers the Unknown → Pending reset path via SSA, and that the
// fake client applies the resulting status patch.
func TestReconcile_InvalidPhase_ResetsToPending(t *testing.T) {
	mc := makeTestMC("llama", "some-invalid-phase")

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)
	// Unknown → Reset → updateStatus applies the Pending status via SSA.
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhasePending, got.Status.Phase)
}

// TestReconcile_Syncing_JobNotFound_MarksFailed verifies the path where a sync
// job is missing — the reconciler transitions to Failed via SSA patch.
func TestReconcile_Syncing_JobNotFound_MarksFailed(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseSyncing)
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"
	mc.Status.SyncJobName = "missing-job"

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhaseFailed, got.Status.Phase)
	assert.Equal(t, "sync job disappeared: missing-job", got.Status.ErrorMessage)
}
