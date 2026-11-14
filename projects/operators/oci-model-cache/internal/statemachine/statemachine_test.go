package statemachine

import (
	"testing"
	"time"

	"github.com/go-logr/logr/testr"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
)

// --- Fixtures ---

func newMC(phase string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-model",
			Namespace: "default",
		},
		Status: v1alpha1.ModelCacheStatus{
			Phase: phase,
		},
	}
}

func validResolveResult() (string, string, string, string, int, int64) {
	return "ghcr.io/jomcgi/models/llama:main", "sha256:abc123", "main", "safetensors", 3, 1024
}

// --- Calculator tests ---

func TestCalculator_EmptyPhase_ReturnsPending(t *testing.T) {
	mc := newMC("")
	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	if _, ok := state.(ModelCachePending); !ok {
		t.Errorf("expected ModelCachePending, got %T", state)
	}
}

func TestCalculator_PendingPhase(t *testing.T) {
	mc := newMC(PhasePending)
	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	if _, ok := state.(ModelCachePending); !ok {
		t.Errorf("expected ModelCachePending, got %T", state)
	}
}

func TestCalculator_UnknownPhase_ReturnsUnknown(t *testing.T) {
	mc := newMC("SomeInvalidPhase")
	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	unknown, ok := state.(ModelCacheUnknown)
	if !ok {
		t.Errorf("expected ModelCacheUnknown, got %T", state)
	}
	if unknown.ObservedPhase != "SomeInvalidPhase" {
		t.Errorf("expected ObservedPhase=SomeInvalidPhase, got %q", unknown.ObservedPhase)
	}
}

func TestCalculator_ResolvingPhase_ValidStatus(t *testing.T) {
	mc := newMC(PhaseResolving)
	mc.Status.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
	mc.Status.ResolvedRevision = "main"
	mc.Status.Format = "safetensors"

	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	resolving, ok := state.(ModelCacheResolving)
	if !ok {
		t.Errorf("expected ModelCacheResolving, got %T", state)
	}
	if resolving.ResolvedRef != mc.Status.ResolvedRef {
		t.Errorf("ResolvedRef mismatch: got %q", resolving.ResolvedRef)
	}
}

func TestCalculator_ResolvingPhase_InvalidStatus_FallsBackToUnknown(t *testing.T) {
	// Missing required fields → validation fails → Unknown
	mc := newMC(PhaseResolving)
	// ResolvedRef is empty → Validate() fails

	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	if _, ok := state.(ModelCacheUnknown); !ok {
		t.Errorf("expected ModelCacheUnknown due to invalid status, got %T", state)
	}
}

func TestCalculator_FailedPhase_ValidStatus(t *testing.T) {
	mc := newMC(PhaseFailed)
	mc.Status.ErrorMessage = "repo not found"
	mc.Status.LastState = "Pending"

	calc := NewModelCacheCalculator(testr.New(t))
	state := calc.Calculate(mc)

	failed, ok := state.(ModelCacheFailed)
	if !ok {
		t.Errorf("expected ModelCacheFailed, got %T", state)
	}
	if failed.ErrorMessage != "repo not found" {
		t.Errorf("expected ErrorMessage=%q, got %q", "repo not found", failed.ErrorMessage)
	}
}

// --- Phase constants ---

func TestPhaseConstants(t *testing.T) {
	cases := []string{PhasePending, PhaseResolving, PhaseSyncing, PhaseReady, PhaseFailed, PhaseUnknown}
	for _, phase := range cases {
		if !IsKnownPhase(phase) {
			t.Errorf("phase %q should be known", phase)
		}
	}
	if IsKnownPhase("totally-made-up") {
		t.Error("unknown phase should not be recognized")
	}
	if !IsKnownPhase("") {
		t.Error("empty string should be treated as initial state")
	}
}

// --- Transition: Pending → Resolving ---

func TestPending_Resolved_TransitionsToResolving(t *testing.T) {
	mc := newMC(PhasePending)
	pending := ModelCachePending{resource: mc}

	resolvedRef, digest, revision, format, fileCount, totalSize := validResolveResult()
	next := pending.Resolved(resolvedRef, digest, revision, format, fileCount, totalSize)

	if next.Phase() != PhaseResolving {
		t.Errorf("expected Resolving, got %s", next.Phase())
	}
	if next.ResolvedRef != resolvedRef {
		t.Errorf("ResolvedRef mismatch")
	}
	if next.Digest != digest {
		t.Errorf("Digest mismatch")
	}
	if next.ResolvedRevision != revision {
		t.Errorf("ResolvedRevision mismatch")
	}
	if err := next.Validate(); err != nil {
		t.Errorf("Validate() failed: %v", err)
	}
}

// --- Transition: Pending → Ready (cache hit) ---

func TestPending_CacheHit_TransitionsToReady(t *testing.T) {
	mc := newMC(PhasePending)
	pending := ModelCachePending{resource: mc}

	resolvedRef, digest, revision, format, fileCount, totalSize := validResolveResult()
	next := pending.CacheHit(resolvedRef, digest, revision, format, fileCount, totalSize)

	if next.Phase() != PhaseReady {
		t.Errorf("expected Ready, got %s", next.Phase())
	}
	if err := next.Validate(); err != nil {
		t.Errorf("Validate() failed: %v", err)
	}
}

// --- Transition: Pending → Failed ---

func TestPending_MarkFailed_TransitionsToFailed(t *testing.T) {
	mc := newMC(PhasePending)
	pending := ModelCachePending{resource: mc}

	next := pending.MarkFailed("repo not found", true, PhasePending)

	if next.Phase() != PhaseFailed {
		t.Errorf("expected Failed, got %s", next.Phase())
	}
	if next.ErrorMessage != "repo not found" {
		t.Errorf("ErrorMessage mismatch")
	}
	if !next.Permanent {
		t.Errorf("expected Permanent=true")
	}
	if next.LastState != PhasePending {
		t.Errorf("LastState mismatch")
	}
	if err := next.Validate(); err != nil {
		t.Errorf("Validate() failed: %v", err)
	}
}

// --- Transition: Resolving → Syncing ---

func TestResolving_JobCreated_TransitionsToSyncing(t *testing.T) {
	mc := newMC(PhaseResolving)
	resolvedRef, digest, revision, format, fileCount, totalSize := validResolveResult()
	resolving := ModelCacheResolving{
		resource: mc,
		ResolveResult: ResolveResult{
			ResolvedRef:      resolvedRef,
			Digest:           digest,
			ResolvedRevision: revision,
			Format:           format,
			FileCount:        fileCount,
			TotalSize:        totalSize,
		},
	}

	next := resolving.JobCreated("sync-job-abc")

	if next.Phase() != PhaseSyncing {
		t.Errorf("expected Syncing, got %s", next.Phase())
	}
	if next.SyncJobName != "sync-job-abc" {
		t.Errorf("SyncJobName mismatch")
	}
	// ResolveResult should be carried forward
	if next.ResolvedRef != resolvedRef {
		t.Errorf("ResolvedRef not carried forward")
	}
	if err := next.Validate(); err != nil {
		t.Errorf("Validate() failed: %v", err)
	}
}

// --- Transition: Resolving → Failed ---

func TestResolving_MarkFailed_TransitionsToFailed(t *testing.T) {
	mc := newMC(PhaseResolving)
	resolvedRef, digest, revision, format, fileCount, totalSize := validResolveResult()
	resolving := ModelCacheResolving{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: resolvedRef, Digest: digest, ResolvedRevision: revision, Format: format, FileCount: fileCount, TotalSize: totalSize},
	}

	next := resolving.MarkFailed("network error", false, PhaseResolving)

	if next.Phase() != PhaseFailed {
		t.Errorf("expected Failed, got %s", next.Phase())
	}
	if next.Permanent {
		t.Error("expected Permanent=false for transient error")
	}
}

// --- Transition: Syncing → Ready ---

func TestSyncing_SyncComplete_TransitionsToReady(t *testing.T) {
	mc := newMC(PhaseSyncing)
	syncing := ModelCacheSyncing{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
		SyncJob:       SyncJob{SyncJobName: "sync-job"},
	}

	next := syncing.SyncComplete("ghcr.io/jomcgi/models/llama:main", "sha256:newdigest", "main", "safetensors", 5, 2048)

	if next.Phase() != PhaseReady {
		t.Errorf("expected Ready, got %s", next.Phase())
	}
	if next.Digest != "sha256:newdigest" {
		t.Errorf("Digest mismatch after SyncComplete")
	}
	if err := next.Validate(); err != nil {
		t.Errorf("Validate() failed: %v", err)
	}
}

// --- Transition: Syncing → Failed ---

func TestSyncing_MarkFailed_TransitionsToFailed(t *testing.T) {
	mc := newMC(PhaseSyncing)
	syncing := ModelCacheSyncing{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main", Format: "gguf"},
		SyncJob:       SyncJob{SyncJobName: "sync-job"},
	}

	next := syncing.MarkFailed("job failed", false, PhaseSyncing)

	if next.Phase() != PhaseFailed {
		t.Errorf("expected Failed, got %s", next.Phase())
	}
}

// --- Transition: Ready → Pending (resync) ---

func TestReady_Resync_TransitionsToPending(t *testing.T) {
	mc := newMC(PhaseReady)
	ready := ModelCacheReady{
		resource:      mc,
		ResolveResult: ResolveResult{ResolvedRef: "ref", Digest: "sha256:abc", ResolvedRevision: "main", Format: "safetensors"},
	}

	next := ready.Resync()

	if next.Phase() != PhasePending {
		t.Errorf("expected Pending, got %s", next.Phase())
	}
}

// --- Transition: Failed → Pending (retry with guard) ---

func TestFailed_Retry_TransitionsToPending_WhenRetryable(t *testing.T) {
	mc := newMC(PhaseFailed)
	failed := ModelCacheFailed{
		resource:  mc,
		ErrorInfo: ErrorInfo{Permanent: false, LastState: PhasePending, ErrorMessage: "transient"},
	}

	next := failed.Retry()

	if next == nil {
		t.Fatal("Retry() should return non-nil for non-permanent failures")
	}
	if next.Phase() != PhasePending {
		t.Errorf("expected Pending after retry, got %s", next.Phase())
	}
}

func TestFailed_Retry_ReturnsNil_WhenPermanent(t *testing.T) {
	mc := newMC(PhaseFailed)
	failed := ModelCacheFailed{
		resource:  mc,
		ErrorInfo: ErrorInfo{Permanent: true, LastState: PhasePending, ErrorMessage: "permanent error"},
	}

	next := failed.Retry()

	if next != nil {
		t.Error("Retry() should return nil for permanent failures")
	}
}

func TestFailed_IsRetryable(t *testing.T) {
	mc := newMC(PhaseFailed)
	failed := ModelCacheFailed{
		resource:  mc,
		ErrorInfo: ErrorInfo{Permanent: false, LastState: "Pending", ErrorMessage: "err"},
	}
	if !failed.IsRetryable() {
		t.Error("expected IsRetryable=true")
	}
}

// --- Transition: Unknown → Pending (reset) ---

func TestUnknown_Reset_TransitionsToPending(t *testing.T) {
	mc := newMC("garbage-phase")
	unknown := ModelCacheUnknown{resource: mc, ObservedPhase: "garbage-phase"}

	next := unknown.Reset()

	if next.Phase() != PhasePending {
		t.Errorf("expected Pending after reset, got %s", next.Phase())
	}
}

func TestUnknown_IsRetryable(t *testing.T) {
	unknown := ModelCacheUnknown{resource: newMC("bad"), ObservedPhase: "bad"}
	if !unknown.IsRetryable() {
		t.Error("expected Unknown to always be retryable")
	}
}

// --- Validate tests ---

func TestResolveResult_Validate_MissingFields(t *testing.T) {
	cases := []struct {
		name string
		r    ResolveResult
	}{
		{"missing resolvedRef", ResolveResult{ResolvedRevision: "main", Format: "gguf"}},
		{"missing resolvedRevision", ResolveResult{ResolvedRef: "ref", Format: "gguf"}},
		{"missing format", ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.r.Validate(); err == nil {
				t.Errorf("expected validation error for %q", tc.name)
			}
		})
	}
}

func TestModelCacheReady_Validate_RequiresDigest(t *testing.T) {
	mc := newMC(PhaseReady)
	ready := ModelCacheReady{
		resource: mc,
		ResolveResult: ResolveResult{
			ResolvedRef:      "ref",
			ResolvedRevision: "main",
			Format:           "gguf",
			// Digest intentionally empty
		},
	}
	if err := ready.Validate(); err == nil {
		t.Error("expected validation error when Digest is missing in Ready state")
	}
}

func TestModelCacheUnknown_Validate_RequiresObservedPhase(t *testing.T) {
	unknown := ModelCacheUnknown{resource: newMC(""), ObservedPhase: ""}
	if err := unknown.Validate(); err == nil {
		t.Error("expected validation error when ObservedPhase is empty")
	}
}

// --- RequeueAfter ---

func TestRequeueAfter_Values(t *testing.T) {
	mc := newMC("")
	cases := []struct {
		state    ModelCacheState
		minDelay time.Duration
	}{
		{ModelCachePending{resource: mc}, 0},
		{ModelCacheResolving{resource: mc}, 5 * time.Second},
		{ModelCacheSyncing{resource: mc}, 10 * time.Second},
		{ModelCacheReady{resource: mc}, 1 * time.Hour},
		{ModelCacheFailed{resource: mc}, 1 * time.Second},
	}
	for _, tc := range cases {
		if tc.state.RequeueAfter() < tc.minDelay {
			t.Errorf("%T.RequeueAfter() = %v, want >= %v", tc.state, tc.state.RequeueAfter(), tc.minDelay)
		}
	}
}

// --- Visitor tests ---

func TestVisit_DispatchesCorrectly(t *testing.T) {
	mc := newMC("")
	states := []ModelCacheState{
		ModelCachePending{resource: mc},
		ModelCacheResolving{resource: mc},
		ModelCacheSyncing{resource: mc},
		ModelCacheReady{resource: mc},
		ModelCacheFailed{resource: mc},
		ModelCacheUnknown{resource: mc, ObservedPhase: "x"},
	}
	expected := []string{"pending", "resolving", "syncing", "ready", "failed", "unknown"}

	visitor := &ModelCacheFuncVisitor[string]{
		OnPending:   func(_ ModelCachePending) string { return "pending" },
		OnResolving: func(_ ModelCacheResolving) string { return "resolving" },
		OnSyncing:   func(_ ModelCacheSyncing) string { return "syncing" },
		OnReady:     func(_ ModelCacheReady) string { return "ready" },
		OnFailed:    func(_ ModelCacheFailed) string { return "failed" },
		OnUnknown:   func(_ ModelCacheUnknown) string { return "unknown" },
	}

	for i, state := range states {
		got := Visit(state, visitor)
		if got != expected[i] {
			t.Errorf("Visit(%T) = %q, want %q", state, got, expected[i])
		}
	}
}

// --- HasSpecChanged / UpdateObservedGeneration ---

func TestHasSpecChanged(t *testing.T) {
	mc := newMC(PhasePending)
	mc.Generation = 3
	mc.Status.ObservedGeneration = 2

	if !HasSpecChanged(mc) {
		t.Error("expected HasSpecChanged=true when generation != observedGeneration")
	}

	mc.Status.ObservedGeneration = 3
	if HasSpecChanged(mc) {
		t.Error("expected HasSpecChanged=false when generation == observedGeneration")
	}
}

func TestUpdateObservedGeneration(t *testing.T) {
	mc := newMC(PhasePending)
	mc.Generation = 5
	mc.Status.ObservedGeneration = 3

	updated := UpdateObservedGeneration(mc)

	if updated.Status.ObservedGeneration != 5 {
		t.Errorf("expected observedGeneration=5, got %d", updated.Status.ObservedGeneration)
	}
	// Original should not be mutated
	if mc.Status.ObservedGeneration != 3 {
		t.Error("UpdateObservedGeneration should not mutate the original")
	}
}
