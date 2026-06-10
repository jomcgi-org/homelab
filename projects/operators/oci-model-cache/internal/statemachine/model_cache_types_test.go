/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// model_cache_types_test.go provides targeted coverage for the ModelCache
// state machine types defined in model_cache_types.go:
//
//   - ResolveResult.Validate() – error ordering and exact messages
//   - SyncJob.Validate() – error message verbatim
//   - ErrorInfo.Validate() – error ordering and exact messages
//   - ModelCacheReady.Validate() – Digest-required check (two-level validation)
//   - ModelCacheUnknown.Validate() – ObservedPhase required check
//   - All states: Phase(), RequeueAfter(), Resource() identity
//   - Validate() pass/fail for every concrete state

package statemachine

import (
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// newMCForTypes returns a minimal *v1alpha1.ModelCache for types test state construction.
func newMCForTypes() *v1alpha1.ModelCache {
	return &v1alpha1.ModelCache{
		ObjectMeta: metav1.ObjectMeta{
			Name: "test-mc",
		},
	}
}

// validRR returns a fully populated ResolveResult for use in states that need it.
func validRR() ResolveResult {
	return ResolveResult{
		ResolvedRef:      "ghcr.io/jomcgi/models/llama:rev-abc",
		ResolvedRevision: "abc123",
		Format:           "gguf",
		TotalSize:        1024,
		Digest:           "sha256:deadbeef",
		FileCount:        1,
	}
}

// =============================================================================
// ResolveResult.Validate
// =============================================================================

var _ = Describe("ResolveResult Validate", func() {
	It("returns format error first when all fields are empty (alphabetical check order)", func() {
		rr := ResolveResult{}
		err := rr.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("format is required"))
	})

	It("returns resolvedRevision error when ResolvedRef and Format are set", func() {
		rr := ResolveResult{ResolvedRef: "ghcr.io/jomcgi/models/llama:main", Format: "gguf"}
		err := rr.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("resolvedRevision is required"))
	})

	It("returns format error when ResolvedRef and ResolvedRevision are set but Format is empty", func() {
		rr := ResolveResult{
			ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
			ResolvedRevision: "abc123",
		}
		err := rr.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("format is required"))
	})

	It("succeeds when ResolvedRef, ResolvedRevision, and Format are all set", func() {
		rr := ResolveResult{
			ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
			ResolvedRevision: "abc123",
			Format:           "safetensors",
		}
		Expect(rr.Validate()).To(Succeed())
	})

	It("does NOT require Digest (Digest is validated separately by Ready state)", func() {
		rr := ResolveResult{
			ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
			ResolvedRevision: "abc123",
			Format:           "gguf",
			// Digest intentionally absent
		}
		Expect(rr.Validate()).To(Succeed())
	})

	It("accepts zero-valued TotalSize and FileCount (they are not validated)", func() {
		rr := ResolveResult{
			ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
			ResolvedRevision: "abc123",
			Format:           "gguf",
			TotalSize:        0,
			FileCount:        0,
		}
		Expect(rr.Validate()).To(Succeed())
	})
})

// =============================================================================
// SyncJob.Validate
// =============================================================================

var _ = Describe("SyncJob Validate", func() {
	It("returns syncJobName error when SyncJobName is empty", func() {
		sj := SyncJob{}
		err := sj.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("syncJobName is required"))
	})

	It("succeeds when SyncJobName is non-empty", func() {
		sj := SyncJob{SyncJobName: "sync-job-12345"}
		Expect(sj.Validate()).To(Succeed())
	})
})

// =============================================================================
// ErrorInfo.Validate
// =============================================================================

var _ = Describe("ErrorInfo Validate", func() {
	It("returns errorMessage error first when both fields are empty (alphabetical check order)", func() {
		ei := ErrorInfo{}
		err := ei.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("errorMessage is required"))
	})

	It("returns errorMessage error when only ErrorMessage is missing", func() {
		ei := ErrorInfo{LastState: "Resolving"}
		err := ei.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("errorMessage is required"))
	})

	It("succeeds when both LastState and ErrorMessage are set", func() {
		ei := ErrorInfo{
			LastState:    "Resolving",
			ErrorMessage: "timeout",
		}
		Expect(ei.Validate()).To(Succeed())
	})

	It("Permanent field does not affect validation", func() {
		// Both Permanent=true and Permanent=false should pass when required fields present
		for _, permanent := range []bool{true, false} {
			ei := ErrorInfo{
				Permanent:    permanent,
				LastState:    "Syncing",
				ErrorMessage: "disk full",
			}
			Expect(ei.Validate()).To(Succeed(), "Permanent=%v should not affect validation", permanent)
		}
	})
})

// =============================================================================
// ModelCachePending
// =============================================================================

var _ = Describe("ModelCachePending", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Pending", func() {
		Expect(ModelCachePending{resource: mc}.Phase()).To(Equal(PhasePending))
	})

	It("RequeueAfter returns 0", func() {
		Expect(ModelCachePending{resource: mc}.RequeueAfter()).To(Equal(time.Duration(0)))
	})

	It("Resource returns the same pointer", func() {
		Expect(ModelCachePending{resource: mc}.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate always succeeds", func() {
		Expect(ModelCachePending{resource: mc}.Validate()).To(Succeed())
	})
})

// =============================================================================
// ModelCacheResolving
// =============================================================================

var _ = Describe("ModelCacheResolving", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Resolving", func() {
		s := ModelCacheResolving{resource: mc, ResolveResult: validRR()}
		Expect(s.Phase()).To(Equal(PhaseResolving))
	})

	It("RequeueAfter returns 10s", func() {
		s := ModelCacheResolving{resource: mc, ResolveResult: validRR()}
		Expect(s.RequeueAfter()).To(Equal(10 * time.Second))
	})

	It("Resource returns the same pointer", func() {
		s := ModelCacheResolving{resource: mc, ResolveResult: validRR()}
		Expect(s.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate succeeds with a complete ResolveResult", func() {
		s := ModelCacheResolving{resource: mc, ResolveResult: validRR()}
		Expect(s.Validate()).To(Succeed())
	})

	It("Validate fails when ResolveResult is incomplete", func() {
		s := ModelCacheResolving{resource: mc}
		Expect(s.Validate()).To(HaveOccurred())
	})
})

// =============================================================================
// ModelCacheSyncing
// =============================================================================

var _ = Describe("ModelCacheSyncing", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Syncing", func() {
		s := ModelCacheSyncing{
			resource:      mc,
			ResolveResult: validRR(),
			SyncJob:       SyncJob{SyncJobName: "job-1"},
		}
		Expect(s.Phase()).To(Equal(PhaseSyncing))
	})

	It("RequeueAfter returns 30s", func() {
		s := ModelCacheSyncing{
			resource:      mc,
			ResolveResult: validRR(),
			SyncJob:       SyncJob{SyncJobName: "job-1"},
		}
		Expect(s.RequeueAfter()).To(Equal(30 * time.Second))
	})

	It("Resource returns the same pointer", func() {
		s := ModelCacheSyncing{
			resource:      mc,
			ResolveResult: validRR(),
			SyncJob:       SyncJob{SyncJobName: "job-1"},
		}
		Expect(s.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate succeeds with complete ResolveResult and SyncJob", func() {
		s := ModelCacheSyncing{
			resource:      mc,
			ResolveResult: validRR(),
			SyncJob:       SyncJob{SyncJobName: "job-1"},
		}
		Expect(s.Validate()).To(Succeed())
	})

	It("Validate fails when ResolveResult is incomplete", func() {
		s := ModelCacheSyncing{
			resource: mc,
			SyncJob:  SyncJob{SyncJobName: "job-1"},
			// ResolveResult empty
		}
		Expect(s.Validate()).To(HaveOccurred())
	})

	It("Validate fails when SyncJob.SyncJobName is empty", func() {
		s := ModelCacheSyncing{
			resource:      mc,
			ResolveResult: validRR(),
			// SyncJob empty
		}
		err := s.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("syncJobName is required"))
	})
})

// =============================================================================
// ModelCacheReady – the unique two-level Digest check
// =============================================================================

var _ = Describe("ModelCacheReady Validate", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Ready", func() {
		s := ModelCacheReady{resource: mc, ResolveResult: validRR()}
		Expect(s.Phase()).To(Equal(PhaseReady))
	})

	It("RequeueAfter returns 6h (21600s)", func() {
		s := ModelCacheReady{resource: mc, ResolveResult: validRR()}
		Expect(s.RequeueAfter()).To(Equal(6 * time.Hour))
	})

	It("Resource returns the same pointer", func() {
		s := ModelCacheReady{resource: mc, ResolveResult: validRR()}
		Expect(s.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate succeeds when all ResolveResult fields and Digest are set", func() {
		s := ModelCacheReady{resource: mc, ResolveResult: validRR()}
		Expect(s.Validate()).To(Succeed())
	})

	It("Validate fails when ResolveResult.Digest is empty (even if other fields are complete)", func() {
		rr := validRR()
		rr.Digest = ""
		s := ModelCacheReady{resource: mc, ResolveResult: rr}
		err := s.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("digest is required for Ready state"))
	})

	It("Validate fails when ResolveResult base fields fail (Format check fires first)", func() {
		// Empty ResolveResult → format error (alphabetical check order)
		// fires before the Digest check
		s := ModelCacheReady{resource: mc}
		err := s.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("format is required"))
	})

	It("Validate error ordering: ResolveResult fields checked before Digest", func() {
		// ResolvedRef and Format present, ResolvedRevision missing →
		// resolvedRevision error (not digest error, proving base group
		// validation runs before the state-scoped digest requirement)
		s := ModelCacheReady{
			resource: mc,
			ResolveResult: ResolveResult{
				ResolvedRef: "ghcr.io/jomcgi/models/llama:main",
				Format:      "gguf",
				// ResolvedRevision and Digest missing
			},
		}
		err := s.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("resolvedRevision is required"))
	})
})

// =============================================================================
// ModelCacheFailed
// =============================================================================

var _ = Describe("ModelCacheFailed", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Failed", func() {
		s := ModelCacheFailed{
			resource:  mc,
			ErrorInfo: ErrorInfo{LastState: "x", ErrorMessage: "y"},
		}
		Expect(s.Phase()).To(Equal(PhaseFailed))
	})

	It("RequeueAfter returns 300s", func() {
		s := ModelCacheFailed{
			resource:  mc,
			ErrorInfo: ErrorInfo{LastState: "x", ErrorMessage: "y"},
		}
		Expect(s.RequeueAfter()).To(Equal(300 * time.Second))
	})

	It("Resource returns the same pointer", func() {
		s := ModelCacheFailed{resource: mc}
		Expect(s.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate succeeds with complete ErrorInfo", func() {
		s := ModelCacheFailed{
			resource:  mc,
			ErrorInfo: ErrorInfo{LastState: "Syncing", ErrorMessage: "disk full"},
		}
		Expect(s.Validate()).To(Succeed())
	})

	It("Validate fails with empty ErrorInfo", func() {
		s := ModelCacheFailed{resource: mc}
		Expect(s.Validate()).To(HaveOccurred())
	})
})

// =============================================================================
// ModelCacheUnknown
// =============================================================================

var _ = Describe("ModelCacheUnknown", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() { mc = newMCForTypes() })

	It("Phase returns Unknown", func() {
		s := ModelCacheUnknown{resource: mc, ObservedPhase: "x"}
		Expect(s.Phase()).To(Equal(PhaseUnknown))
	})

	It("RequeueAfter returns 0", func() {
		s := ModelCacheUnknown{resource: mc, ObservedPhase: "x"}
		Expect(s.RequeueAfter()).To(Equal(time.Duration(0)))
	})

	It("Resource returns the same pointer", func() {
		s := ModelCacheUnknown{resource: mc, ObservedPhase: "x"}
		Expect(s.Resource()).To(BeIdenticalTo(mc))
	})

	It("Validate fails when ObservedPhase is empty", func() {
		s := ModelCacheUnknown{resource: mc}
		err := s.Validate()
		Expect(err).To(HaveOccurred())
		Expect(err.Error()).To(Equal("observedPhase is required for Unknown state"))
	})

	It("Validate succeeds when ObservedPhase is non-empty", func() {
		s := ModelCacheUnknown{resource: mc, ObservedPhase: "SomeOldPhase"}
		Expect(s.Validate()).To(Succeed())
	})
})

// =============================================================================
// All-states RequeueAfter table
// =============================================================================

var _ = Describe("ModelCache state RequeueAfter values", func() {
	var mc *v1alpha1.ModelCache
	BeforeEach(func() { mc = newMCForTypes() })

	DescribeTable("RequeueAfter returns the expected duration for each state",
		func(state ModelCacheState, expected time.Duration) {
			Expect(state.RequeueAfter()).To(Equal(expected))
		},
		Entry("Pending → 0",
			ModelCachePending{resource: mc},
			time.Duration(0)),
		Entry("Resolving → 10s",
			ModelCacheResolving{resource: mc, ResolveResult: validRR()},
			10*time.Second),
		Entry("Syncing → 30s",
			ModelCacheSyncing{resource: mc, ResolveResult: validRR(), SyncJob: SyncJob{SyncJobName: "j"}},
			30*time.Second),
		Entry("Ready → 6h",
			ModelCacheReady{resource: mc, ResolveResult: validRR()},
			6*time.Hour),
		Entry("Failed → 300s",
			ModelCacheFailed{resource: mc, ErrorInfo: ErrorInfo{LastState: "x", ErrorMessage: "y"}},
			300*time.Second),
		Entry("Unknown → 0",
			ModelCacheUnknown{resource: mc, ObservedPhase: "x"},
			time.Duration(0)),
	)
})
