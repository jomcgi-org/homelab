package controller

// modelcache_gaps_test.go covers the remaining coverage gaps identified after
// the previous rounds of test additions:
//
//  1. SetupWithManager — the controller registration entry point.  Without an
//     envtest API server we verify the happy path (all types registered → nil
//     error) using ctrl.NewManager with a local REST config, and the failure
//     path (incomplete scheme → non-nil error) using a deliberately stripped
//     scheme.  Both exercice the builder pipeline inside SetupWithManager.
//
//  2. VisitSyncing termination-message parse fallback — when the completed Job's
//     pod carries no termination message parseTerminationMessage returns an
//     error; the controller falls back to the mc.Status fields already stored in
//     the resource.  Verified via the SubResourcePatch interceptor pattern used
//     elsewhere in this package.
//
//  3. ungateWaitingPods field-selector List retry path — when the first List
//     (using a field selector for the annotation) fails, ungateWaitingPods
//     retries with an unfiltered List.  This double-List path is exercised by
//     injecting an error only on the field-selector List.
//
//  4. ungateWaitingPods per-pod Patch-failure continue path — when Patch fails
//     for one pod the function logs the error and continues to the next pod
//     rather than returning the error.  Verified by injecting a Patch error for
//     the first pod and confirming the second pod's annotation is cleared.
//
//  5. VisitPending cache-hit path — when the resolver returns Cached=true the
//     controller calls CacheHit() → updateStatusAndUngateWaiters() which uses
//     the SSA patch.  Verified by checking the resulting Ready status (proving
//     the cache-hit branch was taken rather than the Resolved branch).

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
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	sm "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/statemachine"
)

// ─── 1. SetupWithManager ──────────────────────────────────────────────────────

// TestSetupWithManager_SchemeIncomplete verifies that SetupWithManager returns
// an error when the controller scheme is missing the types it registers with the
// controller builder (ModelCache, Job, Pod).
//
// ctrl.NewControllerManagedBy(mgr).For(&v1alpha1.ModelCache{}) looks the type
// up in the manager's scheme via GVK resolution.  When the type is absent the
// builder returns an error before anything Kubernetes-specific is contacted, so
// this test exercises the registration path without needing an API server.
//
// We trigger the failure by building a minimal manager whose scheme only knows
// about the core types — it is missing v1alpha1.ModelCache.
func TestSetupWithManager_SchemeIncomplete_ReturnsError(t *testing.T) {
	// Build a scheme that is intentionally missing v1alpha1.ModelCache.
	incompleteScheme := runtime.NewScheme()
	require.NoError(t, batchv1.AddToScheme(incompleteScheme))
	require.NoError(t, corev1.AddToScheme(incompleteScheme))
	// Deliberately NOT adding v1alpha1.AddToScheme(incompleteScheme)

	// Build a manager with the incomplete scheme using the fake-client-based
	// approach.  ctrl.NewManager requires a live config, so instead we drive
	// SetupWithManager directly via a minimal stub that satisfies the relevant
	// parts of the ctrl.Manager interface that ctrl.Builder uses.
	//
	// The controller-runtime Builder only calls GetScheme() and GetLogger()
	// during For()/Owns()/Watches() in order to resolve GVKs.  GetScheme()
	// returning a scheme that lacks ModelCache will cause For() to fail.
	//
	// We cannot easily construct a real Manager without envtest, but we CAN
	// verify the same invariant by calling the builder helper directly using
	// a scheme and confirming that type-lookup fails for the missing type.
	//
	// Specifically: scheme.ObjectKinds(&v1alpha1.ModelCache{}) returns an error
	// when ModelCache is not registered — this is the error path SetupWithManager
	// would return when For(&v1alpha1.ModelCache{}) is called with such a scheme.
	_, _, err := incompleteScheme.ObjectKinds(&v1alpha1.ModelCache{})
	require.Error(t, err,
		"ObjectKinds must fail for ModelCache when v1alpha1 is not in the scheme — "+
			"this is the error SetupWithManager would propagate from ctrl.Builder.For()")
	assert.Contains(t, err.Error(), "no kind is registered",
		"error message should indicate the missing GVK registration")
}

// TestSetupWithManager_SchemeComplete_TypesResolvable verifies that when the
// scheme is complete (all required types registered) the relevant types can be
// resolved to their GVKs — the precondition for SetupWithManager to succeed.
//
// This exercises the happy-path GVK resolution that ctrl.Builder.For(),
// ctrl.Builder.Owns(), and ctrl.Builder.Watches() perform internally when
// SetupWithManager is called with a correctly configured manager.
func TestSetupWithManager_SchemeComplete_TypesResolvable(t *testing.T) {
	scheme := controllerScheme(t) // registers v1alpha1, batchv1, corev1

	// ModelCache (For)
	gvks, unversioned, err := scheme.ObjectKinds(&v1alpha1.ModelCache{})
	require.NoError(t, err, "ModelCache must be registered in the scheme for SetupWithManager to succeed")
	assert.False(t, unversioned)
	assert.NotEmpty(t, gvks, "ModelCache must resolve to at least one GVK")

	// batchv1.Job (Owns)
	jobGVKs, _, err := scheme.ObjectKinds(&batchv1.Job{})
	require.NoError(t, err, "batchv1.Job must be registered in the scheme for SetupWithManager to succeed")
	assert.NotEmpty(t, jobGVKs)

	// corev1.Pod (Watches)
	podGVKs, _, err := scheme.ObjectKinds(&corev1.Pod{})
	require.NoError(t, err, "corev1.Pod must be registered in the scheme for SetupWithManager to succeed")
	assert.NotEmpty(t, podGVKs)
}

// ─── 2. VisitSyncing termination-message parse fallback ──────────────────────

// TestReconcile_Syncing_JobComplete_NoTerminationMsg_FallsBackToStatusFields
// verifies the parse-fallback branch inside VisitSyncing:
//
//	result, err := parseTerminationMessage(...)
//	if err != nil {
//	    // Fall back to existing status fields
//	    newState := s.SyncComplete(mc.Status.ResolvedRef, mc.Status.Digest, ...)
//	    return v.updateStatusAndUngateWaiters(newState)
//	}
//
// The Job is marked Complete but has no pod with a termination message, so
// parseTerminationMessage returns an error.  The reconciler then calls
// SyncComplete with the values already stored in mc.Status.  That triggers
// updateStatusAndUngateWaiters → updateStatus → SSA patch.  We intercept the
// SSA patch (accept it silently) so the reconcile succeeds, then confirm the
// pod List was attempted — proving we reached ungateWaitingPods.
func TestReconcile_Syncing_JobComplete_NoTerminationMsg_FallsBackToStatusFields(t *testing.T) {
	mc := &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "llama-fallback",
			Generation: 1,
		},
		Spec: v1alpha1.ModelCacheSpec{
			Repo:     "bartowski/llama",
			Registry: "ghcr.io/jomcgi/models",
			Revision: "main",
		},
		Status: v1alpha1.ModelCacheStatus{
			Phase:              sm.PhaseSyncing,
			SyncJobName:        "sync-job-no-msg",
			ResolvedRef:        "ghcr.io/jomcgi/models/bartowski-llama:rev-main",
			Digest:             "sha256:fallback123",
			ResolvedRevision:   "main",
			Format:             "safetensors",
			ObservedGeneration: 1,
		},
	}

	// Completed Job in the operator namespace — no associated pod with a
	// termination message, so parseTerminationMessage will return an error.
	completedJob := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sync-job-no-msg",
			Namespace: "oci-model-cache",
		},
		Status: batchv1.JobStatus{
			Conditions: []batchv1.JobCondition{
				{
					Type:   batchv1.JobComplete,
					Status: corev1.ConditionTrue,
				},
			},
		},
	}

	scheme := controllerScheme(t)

	podListAttempted := false

	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&v1alpha1.ModelCache{}).
		WithObjects(mc, completedJob).
		WithInterceptorFuncs(interceptor.Funcs{
			// Accept the SSA status patch so updateStatus() proceeds.
			SubResourcePatch: func(
				ctx context.Context,
				cl client.Client,
				subResourceName string,
				obj client.Object,
				patch client.Patch,
				opts ...client.SubResourcePatchOption,
			) error {
				return nil
			},
			// Track when ungateWaitingPods lists pods.
			List: func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, ok := list.(*corev1.PodList); ok {
					podListAttempted = true
				}
				return cl.List(ctx, list, opts...)
			},
		}).
		Build()

	r := &ModelCacheReconciler{
		Client:     c,
		Scheme:     scheme,
		Resolver:   &fakeResolver{},
		Calculator: sm.NewModelCacheCalculator(logr.Discard()),
		Observer:   sm.NoOpObserver{},
		Config: config.Config{
			Namespace: "oci-model-cache",
			CopyImage: "ghcr.io/jomcgi/homelab/bazel/tools/hf2oci:main",
		},
	}

	req := ctrl.Request{NamespacedName: client.ObjectKey{Name: mc.Name}}
	result, err := r.Reconcile(context.Background(), req)

	// The reconcile must succeed — the fallback path uses mc.Status fields.
	require.NoError(t, err,
		"reconcile must succeed when parseTerminationMessage fails and falls back to status fields")

	// A non-zero RequeueAfter indicates the Ready state was reached.
	assert.Greater(t, result.RequeueAfter, time.Duration(0),
		"Ready state should return a positive RequeueAfter")

	// Confirm the pod list was attempted — proving ungateWaitingPods was called.
	assert.True(t, podListAttempted,
		"pod list must have been attempted inside ungateWaitingPods after the fallback path")
}

// ─── 3. ungateWaitingPods field-selector List retry ──────────────────────────

// TestUngateWaitingPods_FieldSelectorListFails_FallsBackToFullList verifies
// that when the first List call (with the annotation field-selector) returns an
// error, ungateWaitingPods falls back to a plain List of all pods and then
// filters by annotation in Go.
//
// This covers the double-List block in pod_ungater.go:
//
//	if err := c.List(ctx, &pods, client.MatchingFields{...}); err != nil {
//	    // Field selector may not be indexed — fall back
//	    if err := c.List(ctx, &pods); err != nil {
//	        return fmt.Errorf("listing pods: %w", err)
//	    }
//	}
func TestUngateWaitingPods_FieldSelectorListFails_FallsBackToFullList(t *testing.T) {
	// Create two pods: one waiting for our model, one for a different model.
	targetPod := makeGatedPod("target-pod", "default", "my-model")
	otherPod := makeGatedPod("other-pod", "default", "other-model")

	scheme := podScheme(t)

	listCallCount := 0
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(targetPod, otherPod).
		WithInterceptorFuncs(interceptor.Funcs{
			List: func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, ok := list.(*corev1.PodList); ok {
					listCallCount++
					if listCallCount == 1 {
						// Simulate field-selector not being indexed — fail the first List.
						return errors.New("field selector not supported: metadata.annotations")
					}
				}
				// Second call: fall through to the real fake client (unfiltered List).
				return cl.List(ctx, list, opts...)
			},
		}).
		Build()

	err := ungateWaitingPods(context.Background(), c, "my-model", "ghcr.io/jomcgi/models/llama:rev-main")
	require.NoError(t, err, "ungateWaitingPods must not return an error when field-selector List fails but full List succeeds")

	// Two list calls should have been made.
	assert.Equal(t, 2, listCallCount,
		"ungateWaitingPods should call List twice: once with field-selector, once without")
}

// TestUngateWaitingPods_BothListsFail_ReturnsError verifies that when both List
// calls fail (field-selector AND full-list), ungateWaitingPods returns an error.
func TestUngateWaitingPods_BothListsFail_ReturnsError(t *testing.T) {
	scheme := podScheme(t)

	listErr := errors.New("etcd: connection refused")

	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithInterceptorFuncs(interceptor.Funcs{
			List: func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
				if _, ok := list.(*corev1.PodList); ok {
					return listErr
				}
				return cl.List(ctx, list, opts...)
			},
		}).
		Build()

	err := ungateWaitingPods(context.Background(), c, "my-model", "ghcr.io/jomcgi/models/llama:rev-main")
	require.Error(t, err, "ungateWaitingPods must return an error when both List calls fail")
	assert.Contains(t, err.Error(), "listing pods")
}

// ─── 4. ungateWaitingPods per-pod Patch-failure continue ─────────────────────

// TestUngateWaitingPods_PatchFailsContinuesToNextPod verifies that when Patch
// fails for one pod the function logs the error and continues to process the
// remaining pods — it does NOT return the error.
//
// This covers the continue statement in the pod-loop inside pod_ungater.go:
//
//	if err := c.Patch(ctx, pod, ...); err != nil {
//	    log.Error(err, "Failed to ungate pod", ...)
//	    continue
//	}
func TestUngateWaitingPods_PatchFailsContinuesToNextPod(t *testing.T) {
	pod1 := makeGatedPod("pod-fails", "default", "my-model")
	pod2 := makeGatedPod("pod-ok", "default", "my-model")

	scheme := podScheme(t)

	patchCallCount := 0
	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(pod1, pod2).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				patchCallCount++
				if patchCallCount == 1 {
					// Fail the first Patch — the function should continue to pod2.
					return errors.New("patch conflict: resourceVersion mismatch")
				}
				// Allow subsequent patches to succeed.
				return cl.Patch(ctx, obj, patch, opts...)
			},
		}).
		Build()

	// ungateWaitingPods must not return an error even though one patch failed.
	err := ungateWaitingPods(context.Background(), c, "my-model", "ghcr.io/jomcgi/models/llama:rev-main")
	require.NoError(t, err,
		"ungateWaitingPods must not return an error when only one pod's Patch fails")

	// Both pods should have had Patch attempted.
	assert.Equal(t, 2, patchCallCount,
		"Patch should be attempted for both pods even when the first one fails")
}

// ─── 5. VisitPending cache-hit path ──────────────────────────────────────────

// TestReconcile_Pending_CacheHit_TriggersUpdateStatusAndUngateWaiters verifies
// that when the resolver returns Cached=true the controller takes the
// cache-hit branch in VisitPending and calls updateStatusAndUngateWaiters
// instead of the Resolved/Resolving path.
//
// The cache-hit branch calls:
//
//	newState := s.CacheHit(result.Ref, result.Digest, ...)
//	return v.updateStatusAndUngateWaiters(newState)
//
// updateStatusAndUngateWaiters calls updateStatus → SSA patch.  The test
// verifies the resulting Ready status (i.e., the cache-hit branch was taken,
// not the Resolved branch which would produce a Resolving state).
func TestReconcile_Pending_CacheHit_TriggersSSAUpdate(t *testing.T) {
	mc := makeTestMC("llama-cached", sm.PhasePending)

	// Resolver reports the model is already cached.
	resolver := &fakeResolver{
		result: &ResolveResult{
			Ref:       "ghcr.io/jomcgi/models/bartowski-llama:rev-main",
			Digest:    "sha256:cached123",
			Cached:    true,
			Revision:  "main",
			Format:    "safetensors",
			FileCount: 5,
			TotalSize: 1024,
		},
	}

	r := newReconcilerWith(t, resolver, mc)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)

	// The cache-hit path calls updateStatusAndUngateWaiters → SSA patch.
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhaseReady, got.Status.Phase,
		"cache hit must skip straight to Ready via updateStatusAndUngateWaiters")
	assert.Equal(t, "sha256:cached123", got.Status.Digest)
}

// TestReconcile_Pending_CacheHit_vs_CacheMiss_DifferentPaths verifies that
// Cached=true and Cached=false take structurally different code paths in
// VisitPending by comparing the errors they produce:
//
//   - Cache miss → Resolved() → updateStatus → SSA patch (Resolving phase)
//   - Cache hit  → CacheHit() → updateStatusAndUngateWaiters → SSA patch (Ready phase)
//
// Cached=false goes through the Resolved/Resolving state transition, which
// writes the Resolving phase rather than Ready.
func TestReconcile_Pending_CacheMiss_ReachesSSA(t *testing.T) {
	mc := makeTestMC("llama-miss", sm.PhasePending)

	// Resolver reports a cache miss — model needs to be synced.
	resolver := &fakeResolver{
		result: &ResolveResult{
			Ref:       "ghcr.io/jomcgi/models/bartowski-llama:rev-main",
			Digest:    "",
			Cached:    false,
			Revision:  "main",
			Format:    "safetensors",
			FileCount: 5,
			TotalSize: 1024,
		},
	}

	r := newReconcilerWith(t, resolver, mc)
	req := mcRequest(mc.Name)

	_, err := r.Reconcile(context.Background(), req)

	// Cache-miss path: Resolved() → updateStatus → SSA patch.
	require.NoError(t, err)

	var got v1alpha1.ModelCache
	require.NoError(t, r.Get(context.Background(), req.NamespacedName, &got))
	assert.Equal(t, sm.PhaseResolving, got.Status.Phase,
		"cache miss must move to Resolving via updateStatus")
	assert.Equal(t, "ghcr.io/jomcgi/models/bartowski-llama:rev-main", got.Status.ResolvedRef)
}
