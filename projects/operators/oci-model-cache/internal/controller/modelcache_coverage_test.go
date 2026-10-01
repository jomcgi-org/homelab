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
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	sm "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/statemachine"
)

// newReconcilerWithClient creates a ModelCacheReconciler backed by the provided client.
func newReconcilerWithClient(t *testing.T, c client.Client, resolver Resolver) *ModelCacheReconciler {
	t.Helper()
	scheme := controllerScheme(t)
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

// makeResolvingMC creates a cluster-scoped ModelCache in the Resolving phase.
// ModelCache is cluster-scoped (no namespace) so ctrl.SetControllerReference
// can cross into the oci-model-cache namespace for the copy Job.
func makeResolvingMC(name string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
			// No Namespace — ModelCache is cluster-scoped.
		},
		Spec: v1alpha1.ModelCacheSpec{
			Repo:     "bartowski/llama",
			Registry: "ghcr.io/jomcgi/models",
			Revision: "main",
		},
		Status: v1alpha1.ModelCacheStatus{
			Phase:            sm.PhaseResolving,
			ResolvedRef:      "ghcr.io/jomcgi/models/bartowski-llama:rev-main",
			ResolvedRevision: "main",
			Format:           "safetensors",
		},
	}
}

// makeReadyMC creates a ModelCache in the Ready phase with all required status fields.
func makeReadyMC(name string) *v1alpha1.ModelCache {
	mc := makeTestMC(name, sm.PhaseReady)
	mc.Generation = 1
	mc.Status.ObservedGeneration = 1
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/bartowski-llama:rev-main"
	mc.Status.Digest = "sha256:abc123"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"
	return mc
}

// --- Test 1: VisitResolving IsAlreadyExists path ---

// TestReconcile_Resolving_JobAlreadyExists verifies that when creating a copy
// Job returns AlreadyExists (e.g. controller restarted mid-reconcile), the
// controller transitions to Syncing via JobCreated rather than failing.
// The transition uses an SSA status patch, which the fake client applies; we
// verify the resulting Syncing status (proving the AlreadyExists branch was
// taken) rather than a generic error from Create.
func TestReconcile_Resolving_JobAlreadyExists(t *testing.T) {
	mc := makeResolvingMC("llama")

	scheme := controllerScheme(t)

	createCalled := false

	fakeBase := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&v1alpha1.ModelCache{}).
		WithObjects(mc).
		Build()

	intercepted := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&v1alpha1.ModelCache{}).
		WithObjects(mc).
		WithInterceptorFuncs(interceptor.Funcs{
			Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				if _, ok := obj.(*batchv1.Job); ok {
					createCalled = true
					return apierrors.NewAlreadyExists(
						schema.GroupResource{Group: "batch", Resource: "jobs"},
						obj.GetName(),
					)
				}
				return fakeBase.Create(ctx, obj, opts...)
			},
		}).
		Build()

	r := newReconcilerWithClient(t, intercepted, &fakeResolver{})
	// ModelCache is cluster-scoped — use empty namespace in the request.
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: mc.Name}}

	_, err := r.Reconcile(context.Background(), req)

	// Verify Create was called for the Job.
	assert.True(t, createCalled, "Create should have been called for the copy Job")

	// After AlreadyExists, the controller calls updateStatus(s.JobCreated(job.Name))
	// which applies the Syncing status via SSA patch.
	require.NoError(t, err, "AlreadyExists must not surface as a reconcile error")

	var got v1alpha1.ModelCache
	require.NoError(t, intercepted.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhaseSyncing, got.Status.Phase)
	assert.NotEmpty(t, got.Status.SyncJobName)
}

// --- Test 2: VisitSyncing isJobFailed path ---

// TestReconcile_Syncing_JobFailed verifies that a copy Job in Failed state
// causes the controller to transition ModelCache to Failed via SSA patch,
// proving the controller walked the isJobFailed → MarkFailed → updateStatus path.
func TestReconcile_Syncing_JobFailed(t *testing.T) {
	mc := makeTestMC("llama", sm.PhaseSyncing)
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:rev-main"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"
	mc.Status.SyncJobName = "sync-job-failed"

	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sync-job-failed",
			Namespace: "oci-model-cache",
		},
		Status: batchv1.JobStatus{
			Conditions: []batchv1.JobCondition{
				{
					Type:    batchv1.JobFailed,
					Status:  corev1.ConditionTrue,
					Message: "BackoffLimitExceeded: job reached backoff limit",
				},
			},
		},
	}

	r := newReconcilerWith(t, &fakeResolver{}, mc, job)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)

	// Failed job → MarkFailed → updateStatus → SSA patch.
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhaseFailed, got.Status.Phase,
		"job failure should trigger SSA status update to Failed phase")
	assert.Equal(t, "BackoffLimitExceeded: job reached backoff limit", got.Status.ErrorMessage)
}

// TestIsJobFailed_TableDriven verifies the isJobFailed helper for all conditions.
func TestIsJobFailed_TableDriven(t *testing.T) {
	tests := []struct {
		name       string
		conditions []batchv1.JobCondition
		want       bool
	}{
		{
			name:       "Failed=True",
			conditions: []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue}},
			want:       true,
		},
		{
			name:       "Failed=False",
			conditions: []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionFalse}},
			want:       false,
		},
		{
			name:       "no conditions",
			conditions: nil,
			want:       false,
		},
		{
			name:       "Complete=True only",
			conditions: []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}},
			want:       false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			job := &batchv1.Job{Status: batchv1.JobStatus{Conditions: tt.conditions}}
			assert.Equal(t, tt.want, isJobFailed(job))
		})
	}
}

// TestJobFailureReason_TableDriven verifies jobFailureReason extracts the message.
func TestJobFailureReason_TableDriven(t *testing.T) {
	tests := []struct {
		name       string
		conditions []batchv1.JobCondition
		want       string
	}{
		{
			name: "extracts message from Failed condition",
			conditions: []batchv1.JobCondition{
				{Type: batchv1.JobFailed, Status: corev1.ConditionTrue, Message: "BackoffLimitExceeded"},
			},
			want: "BackoffLimitExceeded",
		},
		{
			name: "returns unknown failure for empty conditions",
			want: "unknown failure",
		},
		{
			name: "ignores non-failed conditions",
			conditions: []batchv1.JobCondition{
				{Type: batchv1.JobComplete, Status: corev1.ConditionTrue, Message: "Completed"},
			},
			want: "unknown failure",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			job := &batchv1.Job{Status: batchv1.JobStatus{Conditions: tt.conditions}}
			assert.Equal(t, tt.want, jobFailureReason(job))
		})
	}
}

// --- Test 3: VisitReady HasSpecChanged returns true ---

// TestReconcile_Ready_SpecChanged_TriggersResync verifies that when a Ready
// ModelCache has its spec generation bumped (HasSpecChanged = true), the
// controller calls s.Resync() → updateStatus(newState) which uses SSA patch,
// moving the resource back to Pending rather than the "no op" RequeueAfter
// from the no-change path.
func TestReconcile_Ready_SpecChanged_TriggersResync(t *testing.T) {
	mc := makeReadyMC("llama")
	mc.Generation = 2                // spec changed
	mc.Status.ObservedGeneration = 1 // mismatch → HasSpecChanged() = true

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)

	// VisitReady spec-change path: Resync() → updateStatus → SSA patch.
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhasePending, got.Status.Phase,
		"spec change in Ready should trigger SSA resync, not a no-op requeue")
}

// TestReconcile_Ready_SpecUnchanged_RequeuesOnly verifies that Ready with matching
// generation returns a non-zero RequeueAfter without any error or SSA patch.
func TestReconcile_Ready_SpecUnchanged_RequeuesOnly(t *testing.T) {
	mc := makeReadyMC("llama")
	// generation == observedGeneration → HasSpecChanged() = false

	r := newReconcilerWith(t, &fakeResolver{}, mc)
	req := mcRequest(mc.Name)

	result, err := r.Reconcile(context.Background(), req)

	require.NoError(t, err)
	assert.Greater(t, result.RequeueAfter, time.Duration(0),
		"Ready with unchanged spec should requeue after a long interval without error")
	assert.Equal(t, ctrl.Result{RequeueAfter: result.RequeueAfter}, result)
}

// --- Tests 4 & 5: TTLSweeper sweep error paths ---

// TestTTLSweeper_ListError verifies that when List returns an error, sweep logs
// and returns without panicking. The List error branch is at cleanup.go:41.
func TestTTLSweeper_ListError(t *testing.T) {
	scheme := ttlScheme(t)

	listErr := errors.New("etcd: connection refused")

	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithInterceptorFuncs(interceptor.Funcs{
			List: func(ctx context.Context, client client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				return listErr
			},
		}).
		Build()

	sweeper := &TTLSweeper{Client: c, Interval: 0}

	// sweep must not panic; it logs the error and returns.
	assert.NotPanics(t, func() {
		sweeper.sweep(context.Background())
	}, "sweep must not panic when List returns an error")
}

// TestTTLSweeper_DeleteError_ContinuesToNextItem verifies that when Delete fails
// for one expired item, sweep continues and attempts deletion of the remaining
// items. This tests the `continue` at cleanup.go:59.
func TestTTLSweeper_DeleteError_ContinuesToNextItem(t *testing.T) {
	scheme := ttlScheme(t)

	expired1 := makeMC("expired-1", time.Nanosecond, 2*time.Nanosecond)
	expired2 := makeMC("expired-2", time.Nanosecond, 2*time.Nanosecond)

	deleteCount := 0
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(expired1, expired2).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, client client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				deleteCount++
				return errors.New("delete failed: server busy")
			},
		}).
		Build()

	sweeper := &TTLSweeper{Client: c, Interval: 0}

	assert.NotPanics(t, func() {
		sweeper.sweep(context.Background())
	}, "sweep must not panic when Delete returns an error")

	// Both expired items should have had Delete attempted.
	assert.Equal(t, 2, deleteCount,
		"sweep should attempt deletion of all expired items even when deletes fail")
}

// TestTTLSweeper_DeleteError_PartialSuccess verifies that when the first Delete
// fails and the second succeeds, sweep continues and only the second item is removed.
// The interceptor delegates successful deletes back to the underlying fake client.
func TestTTLSweeper_DeleteError_PartialSuccess(t *testing.T) {
	scheme := ttlScheme(t)

	expired1 := makeMC("expired-fail", time.Nanosecond, 2*time.Nanosecond)
	expired2 := makeMC("expired-ok", time.Nanosecond, 2*time.Nanosecond)

	deleteAttempts := 0
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(expired1, expired2).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				deleteAttempts++
				if deleteAttempts == 1 {
					// Fail the first delete only.
					return errors.New("transient error on first delete")
				}
				// Delegate the second+ deletes to the underlying fake client.
				return cl.Delete(ctx, obj, opts...)
			},
		}).
		Build()

	sweeper := &TTLSweeper{Client: c, Interval: 0}
	sweeper.sweep(context.Background())

	// Both items should have had Delete attempted.
	assert.Equal(t, 2, deleteAttempts,
		"sweep should attempt deletion of both expired items")

	// The second delete succeeded — only one item should remain in the store.
	var remaining v1alpha1.ModelCacheList
	require.NoError(t, c.List(context.Background(), &remaining))
	assert.Len(t, remaining.Items, 1,
		"only the item whose delete failed should remain after partial success")
}
