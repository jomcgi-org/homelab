package statemachine

// model_cache_accessors_test.go directly asserts the Phase(), RequeueAfter(),
// and Resource() accessor methods on every concrete ModelCacheState type.
//
// These table-driven tests cover all six states with populated payloads,
// exact requeue durations, resource pointer identity, and nil resources.
// Other suites retain accessor checks with distinct inputs and after
// validation, construction, or transitions.

import (
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
)

// newMCResource constructs a minimal *v1alpha1.ModelCache for use in accessor
// tests where the concrete resource value matters (Resource() assertions).
func newMCResource(name string) *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
		},
	}
}

// validResolveResultForAccessors builds a fully-populated ResolveResult so
// that state constructors that embed it won't fail Validate().
func validResolveResultForAccessors() ResolveResult {
	return ResolveResult{
		ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
		Digest:           "sha256:deadbeef",
		ResolvedRevision: "main",
		Format:           "safetensors",
		FileCount:        5,
		TotalSize:        1024,
	}
}

// ---- Phase() ----------------------------------------------------------------

// TestPhase_AllStates verifies that every concrete ModelCacheState returns the
// correct phase string constant. Regressions here break the status-writer.
func TestPhase_AllStates(t *testing.T) {
	mc := newMCResource("phase-test")
	rr := validResolveResultForAccessors()

	tests := []struct {
		name  string
		state ModelCacheState
		want  string
	}{
		{
			name:  "Pending",
			state: ModelCachePending{resource: mc},
			want:  PhasePending,
		},
		{
			name:  "Resolving",
			state: ModelCacheResolving{resource: mc, ResolveResult: rr},
			want:  PhaseResolving,
		},
		{
			name: "Syncing",
			state: ModelCacheSyncing{
				resource:      mc,
				ResolveResult: rr,
				SyncJob:       SyncJob{SyncJobName: "sync-job-abc"},
			},
			want: PhaseSyncing,
		},
		{
			name:  "Ready",
			state: ModelCacheReady{resource: mc, ResolveResult: rr},
			want:  PhaseReady,
		},
		{
			name: "Failed",
			state: ModelCacheFailed{
				resource:  mc,
				ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "oops"},
			},
			want: PhaseFailed,
		},
		{
			name:  "Unknown",
			state: ModelCacheUnknown{resource: mc, ObservedPhase: "garbage"},
			want:  PhaseUnknown,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tt.state.Phase()
			if got != tt.want {
				t.Errorf("Phase() = %q, want %q", got, tt.want)
			}
		})
	}
}

// ---- RequeueAfter() ---------------------------------------------------------

// TestRequeueAfter_AllStates verifies the documented requeue intervals for
// every state. These intervals drive the controller's reconcile cadence; a
// wrong value would cause either a busy loop or an excessively long delay.
func TestRequeueAfter_AllStates(t *testing.T) {
	mc := newMCResource("requeue-test")
	rr := validResolveResultForAccessors()

	tests := []struct {
		name  string
		state ModelCacheState
		want  time.Duration
	}{
		{
			name:  "Pending has no requeue (0)",
			state: ModelCachePending{resource: mc},
			want:  0,
		},
		{
			name:  "Resolving requeues after 10s",
			state: ModelCacheResolving{resource: mc, ResolveResult: rr},
			want:  10 * time.Second,
		},
		{
			name: "Syncing requeues after 30s",
			state: ModelCacheSyncing{
				resource:      mc,
				ResolveResult: rr,
				SyncJob:       SyncJob{SyncJobName: "sync-job-abc"},
			},
			want: 30 * time.Second,
		},
		{
			name:  "Ready requeues after 6h",
			state: ModelCacheReady{resource: mc, ResolveResult: rr},
			want:  6 * time.Hour,
		},
		{
			name: "Failed requeues after 5m",
			state: ModelCacheFailed{
				resource:  mc,
				ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "oops"},
			},
			want: 5 * time.Minute,
		},
		{
			name:  "Unknown has no requeue (0)",
			state: ModelCacheUnknown{resource: mc, ObservedPhase: "garbage"},
			want:  0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tt.state.RequeueAfter()
			if got != tt.want {
				t.Errorf("RequeueAfter() = %v, want %v", got, tt.want)
			}
		})
	}
}

// ---- Resource() -------------------------------------------------------------

// TestResource_AllStates verifies that Resource() returns the same pointer
// that was passed to the state constructor. The controller uses Resource() to
// get the live object back from a computed state; returning the wrong pointer
// would silently discard status updates.
func TestResource_AllStates(t *testing.T) {
	rr := validResolveResultForAccessors()

	tests := []struct {
		name string
		stFn func(mc *v1alpha1.ModelCache) ModelCacheState
	}{
		{
			name: "Pending",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCachePending{resource: mc}
			},
		},
		{
			name: "Resolving",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCacheResolving{resource: mc, ResolveResult: rr}
			},
		},
		{
			name: "Syncing",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCacheSyncing{
					resource:      mc,
					ResolveResult: rr,
					SyncJob:       SyncJob{SyncJobName: "sync-job"},
				}
			},
		},
		{
			name: "Ready",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCacheReady{resource: mc, ResolveResult: rr}
			},
		},
		{
			name: "Failed",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCacheFailed{
					resource:  mc,
					ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "oops"},
				}
			},
		},
		{
			name: "Unknown",
			stFn: func(mc *v1alpha1.ModelCache) ModelCacheState {
				return ModelCacheUnknown{resource: mc, ObservedPhase: "garbage"}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			mc := newMCResource("resource-test-" + tt.name)
			state := tt.stFn(mc)
			got := state.Resource()
			if got != mc {
				t.Errorf("Resource() = %p, want same pointer as input %p", got, mc)
			}
		})
	}
}

// TestResource_NilResource verifies that states constructed with a nil resource
// pointer return nil from Resource() — no nil-dereference panic. This guards
// against accidental crashes in tests or edge-case controller code.
func TestResource_NilResource_ReturnNil(t *testing.T) {
	states := []ModelCacheState{
		ModelCachePending{resource: nil},
		ModelCacheResolving{resource: nil},
		ModelCacheSyncing{resource: nil},
		ModelCacheReady{resource: nil},
		ModelCacheFailed{resource: nil},
		ModelCacheUnknown{resource: nil, ObservedPhase: "X"},
	}

	for _, s := range states {
		if got := s.Resource(); got != nil {
			t.Errorf("%T.Resource() with nil resource = %v, want nil", s, got)
		}
	}
}
