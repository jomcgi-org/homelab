package controller

import (
	"context"
	"fmt"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	sm "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/statemachine"
)

// ModelCacheReconciler reconciles a ModelCache object.
type ModelCacheReconciler struct {
	client.Client
	Scheme     *runtime.Scheme
	Resolver   Resolver
	Calculator *sm.ModelCacheCalculator
	Observer   sm.TransitionObserver
	Config     config.Config
}

// +kubebuilder:rbac:groups=oci-model-cache.jomcgi.dev,resources=modelcaches,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=oci-model-cache.jomcgi.dev,resources=modelcaches/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=create;delete;get;list;watch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile moves the ModelCache resource closer to its desired state.
func (r *ModelCacheReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)
	startTime := time.Now()

	// 1. Fetch the resource
	var mc v1alpha1.ModelCache
	if err := r.Get(ctx, req.NamespacedName, &mc); err != nil {
		if errors.IsNotFound(err) {
			log.V(1).Info("ModelCache resource not found, ignoring since object must be deleted")
			sm.CleanupResourceMetrics(req.Namespace, req.Name)
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// 2. Calculate current state from status
	currentState := r.Calculator.Calculate(&mc)
	phase := currentState.Phase()
	log.V(1).Info("Calculated state", "phase", phase)

	// 3. Visit the state to determine next action
	visitor := &modelCacheVisitor{
		reconciler: r,
		ctx:        ctx,
	}
	result := sm.Visit(currentState, visitor)

	// 4. Record metrics
	sm.RecordReconcile(phase, time.Since(startTime), result.Error == nil)

	return result.Result, result.Error
}

// VisitResult is returned by visitor methods.
type VisitResult struct {
	Result ctrl.Result
	Error  error
}

// modelCacheVisitor implements sm.ModelCacheVisitor[VisitResult].
type modelCacheVisitor struct {
	reconciler *ModelCacheReconciler
	ctx        context.Context
}

// Compile-time check that modelCacheVisitor implements the visitor interface.
var _ sm.ModelCacheVisitor[VisitResult] = (*modelCacheVisitor)(nil)

// VisitPending handles the Pending state — resolve the model.
func (v *modelCacheVisitor) VisitPending(s sm.ModelCachePending) VisitResult {
	mc := s.Resource()
	log := logf.FromContext(v.ctx)

	log.Info("Resolving model", "repo", mc.Spec.Repo, "revision", mc.Spec.Revision)

	result, err := v.reconciler.Resolver.Resolve(v.ctx, mc.Spec.Repo, mc.Spec.Registry, mc.Spec.Revision, mc.Spec.File)
	if err != nil {
		return v.handleError(s, err, "Pending")
	}

	if result.Cached {
		// Cache hit — skip directly to Ready
		log.Info("Cache hit", "ref", result.Ref, "digest", result.Digest)
		newState := s.CacheHit(result.Ref, result.Digest, result.Revision, result.Format, result.FileCount, result.TotalSize)
		return v.updateStatusAndUngateWaiters(newState)
	}

	// Model needs to be copied — transition to Resolving
	log.Info("Model not cached, will sync", "ref", result.Ref, "format", result.Format, "files", result.FileCount)
	newState := s.Resolved(result.Ref, result.Digest, result.Revision, result.Format, result.FileCount, result.TotalSize)
	return v.updateStatus(newState)
}

// VisitResolving handles the Resolving state — create a copy Job.
func (v *modelCacheVisitor) VisitResolving(s sm.ModelCacheResolving) VisitResult {
	mc := s.Resource()
	log := logf.FromContext(v.ctx)

	log.Info("Creating copy Job", "repo", mc.Spec.Repo)

	job := buildCopyJob(mc, v.reconciler.Config)
	if err := ctrl.SetControllerReference(mc, job, v.reconciler.Scheme); err != nil {
		return VisitResult{Error: fmt.Errorf("setting owner reference: %w", err)}
	}

	if err := v.reconciler.Create(v.ctx, job); err != nil {
		if errors.IsAlreadyExists(err) {
			// Job already exists — transition with its name
			log.V(1).Info("Copy Job already exists", "job", job.Name)
			newState := s.JobCreated(job.Name)
			return v.updateStatus(newState)
		}
		return v.handleError(s, err, "Resolving")
	}

	log.Info("Copy Job created", "job", job.Name)
	newState := s.JobCreated(job.Name)
	return v.updateStatus(newState)
}

// VisitSyncing handles the Syncing state — watch the copy Job.
func (v *modelCacheVisitor) VisitSyncing(s sm.ModelCacheSyncing) VisitResult {
	mc := s.Resource()
	log := logf.FromContext(v.ctx)

	// Fetch the Job
	var job batchv1.Job
	jobKey := client.ObjectKey{Namespace: v.reconciler.Config.Namespace, Name: s.SyncJobName}
	if err := v.reconciler.Get(v.ctx, jobKey, &job); err != nil {
		if errors.IsNotFound(err) {
			log.Info("Sync Job not found, marking failed", "job", s.SyncJobName)
			newState := s.MarkFailed("sync job disappeared: "+s.SyncJobName, false, "Syncing")
			return v.updateStatus(newState)
		}
		return VisitResult{Error: err}
	}

	// Check Job status
	if isJobComplete(&job) {
		log.Info("Sync Job completed successfully", "job", s.SyncJobName)
		result, err := parseTerminationMessage(v.ctx, v.reconciler.Client, &job)
		if err != nil {
			log.Error(err, "Failed to parse Job termination message, using status fields")
			// Fall back to existing status fields
			newState := s.SyncComplete(
				mc.Status.ResolvedRef, mc.Status.Digest, mc.Status.ResolvedRevision,
				mc.Status.Format, mc.Status.FileCount, mc.Status.TotalSize,
			)
			return v.updateStatusAndUngateWaiters(newState)
		}
		newState := s.SyncComplete(result.Ref, result.Digest, result.Revision, result.Format, result.FileCount, result.TotalSize)
		return v.updateStatusAndUngateWaiters(newState)
	}

	if isJobFailed(&job) {
		reason := jobFailureReason(&job)
		log.Info("Sync Job failed", "job", s.SyncJobName, "reason", reason)
		newState := s.MarkFailed(reason, false, "Syncing")
		return v.updateStatus(newState)
	}

	// Job still running — requeue
	log.V(1).Info("Sync Job still running", "job", s.SyncJobName)
	return VisitResult{Result: ctrl.Result{RequeueAfter: s.RequeueAfter()}}
}

// VisitReady handles the Ready state — check for spec changes or stale mutable revisions.
func (v *modelCacheVisitor) VisitReady(s sm.ModelCacheReady) VisitResult {
	mc := s.Resource()
	log := logf.FromContext(v.ctx)

	// Check for spec changes
	if sm.HasSpecChanged(mc) {
		log.Info("Spec changed, resyncing")
		newState := s.Resync()
		return v.updateStatus(newState)
	}

	// For mutable revisions (like "main"), periodically re-resolve
	if mc.Spec.Revision == "main" || mc.Spec.Revision == "" {
		log.V(1).Info("Mutable revision, will re-check on next requeue")
	}

	log.V(1).Info("Model cache ready", "ref", s.ResolvedRef)
	return VisitResult{Result: ctrl.Result{RequeueAfter: s.RequeueAfter()}}
}

// VisitFailed handles the Failed state — retry if not permanent.
func (v *modelCacheVisitor) VisitFailed(s sm.ModelCacheFailed) VisitResult {
	mc := s.Resource()
	log := logf.FromContext(v.ctx)

	log.Info("In Failed state", "lastState", s.LastState, "permanent", s.Permanent, "error", s.ErrorMessage)

	// Check for spec changes (user may have fixed the spec)
	if sm.HasSpecChanged(mc) {
		log.Info("Spec changed, retrying from Pending")
		// Force retry even if permanent — spec change means new intent
		mc.Status.Phase = sm.PhasePending
		mc.Status.ObservedGeneration = mc.Generation
		mc.Status.ErrorMessage = ""
		mc.Status.LastState = ""
		mc.Status.Permanent = false
		if err := v.reconciler.Status().Update(v.ctx, mc); err != nil {
			return VisitResult{Error: err}
		}
		return VisitResult{Result: ctrl.Result{Requeue: true}}
	}

	// Check if we can retry (non-permanent)
	if newState := s.Retry(); newState != nil {
		log.Info("Retrying from Failed state")
		return v.updateStatus(*newState)
	}

	// Permanent failure — stay in Failed, requeue slowly
	log.Info("Permanent failure, staying in Failed state")
	return VisitResult{Result: ctrl.Result{RequeueAfter: s.RetryBackoff()}}
}

// VisitUnknown handles the Unknown state — reset to Pending.
func (v *modelCacheVisitor) VisitUnknown(s sm.ModelCacheUnknown) VisitResult {
	log := logf.FromContext(v.ctx)
	log.Info("Unknown state detected, resetting to Pending", "observedPhase", s.ObservedPhase)

	newState := s.Reset()
	return v.updateStatus(newState)
}

// Helper methods

// updateStatus updates the resource status with the new state using Server-Side Apply.
func (v *modelCacheVisitor) updateStatus(newState sm.ModelCacheState) VisitResult {
	// Mark the current generation as observed so HasSpecChanged() returns false
	// until the user modifies the spec again.
	newState.Resource().Status.ObservedGeneration = newState.Resource().Generation

	// An SSA apply body needs apiVersion and kind; typed reads can leave TypeMeta empty.
	newState.Resource().SetGroupVersionKind(v1alpha1.GroupVersion.WithKind("ModelCache"))
	patch, err := sm.SSAPatch(newState)
	if err != nil {
		return VisitResult{Error: fmt.Errorf("failed to create SSA patch: %w", err)}
	}

	resource := newState.Resource()
	if err := v.reconciler.Status().Patch(v.ctx, resource, patch, client.FieldOwner(sm.FieldManager), client.ForceOwnership); err != nil {
		return VisitResult{Error: err}
	}

	return VisitResult{Result: ctrl.Result{RequeueAfter: newState.RequeueAfter()}}
}

// updateStatusAndUngateWaiters updates status and also ungates any pods waiting for this model.
func (v *modelCacheVisitor) updateStatusAndUngateWaiters(newState sm.ModelCacheReady) VisitResult {
	result := v.updateStatus(newState)
	if result.Error != nil {
		return result
	}

	// Ungate waiting pods
	mc := newState.Resource()
	if err := ungateWaitingPods(v.ctx, v.reconciler.Client, mc.Name, newState.ResolvedRef); err != nil {
		logf.FromContext(v.ctx).Error(err, "Failed to ungate waiting pods")
		// Don't fail the reconcile — pods will be ungated on next reconcile
	}

	return result
}

// handleError transitions to Failed state for permanent errors or requeues for transient errors.
func (v *modelCacheVisitor) handleError(from sm.ModelCacheState, err error, fromPhase string) VisitResult {
	log := logf.FromContext(v.ctx)

	permanent := IsPermanentError(err)
	if !permanent {
		log.Info("Transient error, will retry", "error", err)
		sm.RecordError("transient")
		return VisitResult{Result: ctrl.Result{RequeueAfter: 30 * time.Second}}
	}

	log.Error(err, "Permanent error, transitioning to Failed")
	sm.RecordError("permanent")

	mc := from.Resource()
	mc.Status.Phase = sm.PhaseFailed
	mc.Status.LastState = fromPhase
	mc.Status.ErrorMessage = err.Error()
	mc.Status.Permanent = true
	mc.Status.ObservedGeneration = mc.Generation

	if updateErr := v.reconciler.Status().Update(v.ctx, mc); updateErr != nil {
		return VisitResult{Error: updateErr}
	}

	return VisitResult{Result: ctrl.Result{RequeueAfter: 5 * time.Minute}}
}

// SetupWithManager sets up the controller with the Manager.
func (r *ModelCacheReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.ModelCache{}).
		Owns(&batchv1.Job{}).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(podToModelCacheRequests)).
		Named("modelcache").
		WithOptions(controller.Options{
			MaxConcurrentReconciles: 3,
		}).
		Complete(r)
}

// podToModelCacheRequests maps a Pod to the ModelCache requests it's waiting for.
func podToModelCacheRequests(ctx context.Context, obj client.Object) []ctrl.Request {
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		return nil
	}

	mcName := pod.Annotations["oci-model-cache.jomcgi.dev/waiting-for"]
	if mcName == "" {
		return nil
	}

	return []ctrl.Request{
		{NamespacedName: client.ObjectKey{Name: mcName}},
	}
}
