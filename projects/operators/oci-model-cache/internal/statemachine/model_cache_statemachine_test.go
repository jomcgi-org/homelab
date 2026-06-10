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

package statemachine

import (
	"testing"
	"time"

	"github.com/go-logr/logr"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestStatemachineGinkgo(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "ModelCache Statemachine Ginkgo Suite")
}

// newModelCache creates a minimal ModelCache for testing.
func newModelCache(phase string) *v1alpha1.ModelCache {
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

// newModelCacheWithStatus creates a ModelCache with full status fields.
func newModelCacheWithStatus(status v1alpha1.ModelCacheStatus) *v1alpha1.ModelCache {
	mc := newModelCache(status.Phase)
	mc.Status = status
	return mc
}

// validResolveArgs returns a full set of valid resolve parameters.
func validResolveArgs() (resolvedRef, digest, resolvedRevision, format string, fileCount int, totalSize int64) {
	return "ghcr.io/jomcgi/models/llama:main", "sha256:abc123def456", "main", "safetensors", 3, 4096
}

// =============================================================================
// Phases
// =============================================================================

var _ = Describe("Phases", func() {
	Describe("AllPhases", func() {
		It("should return all 6 known phases", func() {
			phases := AllPhases()
			Expect(phases).To(HaveLen(6))
			Expect(phases).To(ContainElements(
				PhasePending,
				PhaseResolving,
				PhaseSyncing,
				PhaseReady,
				PhaseFailed,
				PhaseUnknown,
			))
		})
	})

	Describe("IsKnownPhase", func() {
		It("should return true for all known phases", func() {
			for _, phase := range AllPhases() {
				Expect(IsKnownPhase(phase)).To(BeTrue(), "phase %q should be known", phase)
			}
		})

		It("should return true for empty string (initial state)", func() {
			Expect(IsKnownPhase("")).To(BeTrue())
		})

		DescribeTable("should return false for unknown phase strings",
			func(phase string) {
				Expect(IsKnownPhase(phase)).To(BeFalse())
			},
			Entry("invalid phase", "InvalidPhase"),
			Entry("lowercase pending", "pending"),
			Entry("uppercase ready", "READY"),
			Entry("corrupted value", "SomeCorruptedValue"),
			Entry("numeric string", "123"),
			Entry("partial match", "Pend"),
		)
	})
})

// =============================================================================
// Types / Validate
// =============================================================================

var _ = Describe("ResolveResult", func() {
	Describe("Validate", func() {
		It("should pass for fully populated struct", func() {
			r := ResolveResult{
				ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
				Digest:           "sha256:abc",
				ResolvedRevision: "main",
				Format:           "safetensors",
				FileCount:        3,
				TotalSize:        1024,
			}
			Expect(r.Validate()).To(Succeed())
		})

		DescribeTable("should fail when required fields are missing",
			func(r ResolveResult, wantErrSubstr string) {
				err := r.Validate()
				Expect(err).To(HaveOccurred())
				Expect(err.Error()).To(ContainSubstring(wantErrSubstr))
			},
			Entry("missing resolvedRef",
				ResolveResult{ResolvedRevision: "main", Format: "gguf"},
				"resolvedRef",
			),
			Entry("missing resolvedRevision",
				ResolveResult{ResolvedRef: "ref", Format: "gguf"},
				"resolvedRevision",
			),
			Entry("missing format",
				ResolveResult{ResolvedRef: "ref", ResolvedRevision: "main"},
				"format",
			),
			Entry("all fields missing",
				// Generated checks run in alphabetical field order
				ResolveResult{},
				"format",
			),
		)

		It("should pass when Digest is empty (not required by ResolveResult)", func() {
			r := ResolveResult{
				ResolvedRef:      "ref",
				ResolvedRevision: "main",
				Format:           "gguf",
				// Digest intentionally empty
			}
			Expect(r.Validate()).To(Succeed())
		})
	})
})

var _ = Describe("SyncJob", func() {
	Describe("Validate", func() {
		It("should pass for populated struct", func() {
			s := SyncJob{SyncJobName: "sync-job-abc"}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail when SyncJobName is empty", func() {
			s := SyncJob{}
			err := s.Validate()
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("syncJobName"))
		})
	})
})

var _ = Describe("ErrorInfo", func() {
	Describe("Validate", func() {
		It("should pass for populated struct", func() {
			e := ErrorInfo{
				Permanent:    false,
				LastState:    "Pending",
				ErrorMessage: "something went wrong",
			}
			Expect(e.Validate()).To(Succeed())
		})

		DescribeTable("should fail when required fields are missing",
			func(e ErrorInfo, wantErrSubstr string) {
				err := e.Validate()
				Expect(err).To(HaveOccurred())
				Expect(err.Error()).To(ContainSubstring(wantErrSubstr))
			},
			Entry("missing lastState",
				ErrorInfo{ErrorMessage: "err"},
				"lastState",
			),
			Entry("missing errorMessage",
				ErrorInfo{LastState: "Pending"},
				"errorMessage",
			),
			Entry("both missing",
				// Generated checks run in alphabetical field order
				ErrorInfo{},
				"errorMessage",
			),
		)
	})
})

var _ = Describe("State Validate methods", func() {
	mc := newModelCache("")

	Describe("ModelCachePending", func() {
		It("should always validate successfully", func() {
			s := ModelCachePending{resource: mc}
			Expect(s.Validate()).To(Succeed())
		})

		It("should return PhasePending from Phase()", func() {
			s := ModelCachePending{resource: mc}
			Expect(s.Phase()).To(Equal(PhasePending))
		})

		It("should return 0 from RequeueAfter()", func() {
			s := ModelCachePending{resource: mc}
			Expect(s.RequeueAfter()).To(Equal(time.Duration(0)))
		})

		It("should return the underlying resource", func() {
			s := ModelCachePending{resource: mc}
			Expect(s.Resource()).To(Equal(mc))
		})
	})

	Describe("ModelCacheResolving", func() {
		It("should validate successfully with all required fields", func() {
			s := ModelCacheResolving{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "safetensors",
				},
			}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail validation when ResolveResult is incomplete", func() {
			s := ModelCacheResolving{resource: mc}
			Expect(s.Validate()).To(HaveOccurred())
		})

		It("should return PhaseResolving from Phase()", func() {
			s := ModelCacheResolving{resource: mc}
			Expect(s.Phase()).To(Equal(PhaseResolving))
		})

		It("should return a positive duration from RequeueAfter()", func() {
			s := ModelCacheResolving{resource: mc}
			Expect(s.RequeueAfter()).To(BeNumerically(">", 0))
		})
	})

	Describe("ModelCacheSyncing", func() {
		It("should validate successfully with all required fields", func() {
			s := ModelCacheSyncing{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "gguf",
				},
				SyncJob: SyncJob{SyncJobName: "job-123"},
			}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail validation when SyncJob is missing", func() {
			s := ModelCacheSyncing{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "gguf",
				},
			}
			Expect(s.Validate()).To(HaveOccurred())
		})

		It("should fail validation when ResolveResult is incomplete", func() {
			s := ModelCacheSyncing{
				resource: mc,
				SyncJob:  SyncJob{SyncJobName: "job"},
			}
			Expect(s.Validate()).To(HaveOccurred())
		})

		It("should return PhaseSyncing from Phase()", func() {
			s := ModelCacheSyncing{resource: mc}
			Expect(s.Phase()).To(Equal(PhaseSyncing))
		})

		It("should return a positive duration from RequeueAfter()", func() {
			s := ModelCacheSyncing{resource: mc}
			Expect(s.RequeueAfter()).To(BeNumerically(">=", 10*time.Second))
		})
	})

	Describe("ModelCacheReady", func() {
		It("should validate successfully with all required fields including Digest", func() {
			s := ModelCacheReady{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					Digest:           "sha256:abc",
					ResolvedRevision: "main",
					Format:           "safetensors",
				},
			}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail validation when Digest is empty", func() {
			s := ModelCacheReady{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "safetensors",
					// Digest intentionally empty
				},
			}
			err := s.Validate()
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("digest"))
		})

		It("should fail validation when ResolveResult is incomplete", func() {
			s := ModelCacheReady{
				resource:      mc,
				ResolveResult: ResolveResult{Digest: "sha256:abc"},
			}
			Expect(s.Validate()).To(HaveOccurred())
		})

		It("should return PhaseReady from Phase()", func() {
			s := ModelCacheReady{resource: mc}
			Expect(s.Phase()).To(Equal(PhaseReady))
		})

		It("should return a large requeue duration (6h or more)", func() {
			s := ModelCacheReady{resource: mc}
			Expect(s.RequeueAfter()).To(BeNumerically(">=", 6*time.Hour))
		})
	})

	Describe("ModelCacheFailed", func() {
		It("should validate successfully with all required fields", func() {
			s := ModelCacheFailed{
				resource:  mc,
				ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "error"},
			}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail validation when ErrorInfo is incomplete", func() {
			s := ModelCacheFailed{resource: mc}
			Expect(s.Validate()).To(HaveOccurred())
		})

		It("should return PhaseFailed from Phase()", func() {
			s := ModelCacheFailed{resource: mc}
			Expect(s.Phase()).To(Equal(PhaseFailed))
		})

		It("should return a positive duration from RequeueAfter()", func() {
			s := ModelCacheFailed{resource: mc}
			Expect(s.RequeueAfter()).To(BeNumerically(">", 0))
		})
	})

	Describe("ModelCacheUnknown", func() {
		It("should validate successfully when ObservedPhase is set", func() {
			s := ModelCacheUnknown{resource: mc, ObservedPhase: "SomeWeirdPhase"}
			Expect(s.Validate()).To(Succeed())
		})

		It("should fail validation when ObservedPhase is empty", func() {
			s := ModelCacheUnknown{resource: mc, ObservedPhase: ""}
			err := s.Validate()
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("observedPhase"))
		})

		It("should return PhaseUnknown from Phase()", func() {
			s := ModelCacheUnknown{resource: mc, ObservedPhase: "x"}
			Expect(s.Phase()).To(Equal(PhaseUnknown))
		})

		It("should return 0 from RequeueAfter()", func() {
			s := ModelCacheUnknown{resource: mc, ObservedPhase: "x"}
			Expect(s.RequeueAfter()).To(Equal(time.Duration(0)))
		})
	})
})

// =============================================================================
// Transitions
// =============================================================================

var _ = Describe("Transitions", func() {
	Describe("from Pending", func() {
		var pending ModelCachePending
		var mc *v1alpha1.ModelCache

		BeforeEach(func() {
			mc = newModelCache(PhasePending)
			pending = ModelCachePending{resource: mc}
		})

		Describe("Resolved (Pending → Resolving)", func() {
			It("should return a Resolving state with all fields populated", func() {
				ref, digest, rev, format, count, size := validResolveArgs()
				next := pending.Resolved(ref, digest, rev, format, count, size)

				Expect(next.Phase()).To(Equal(PhaseResolving))
				Expect(next.ResolvedRef).To(Equal(ref))
				Expect(next.Digest).To(Equal(digest))
				Expect(next.ResolvedRevision).To(Equal(rev))
				Expect(next.Format).To(Equal(format))
				Expect(next.FileCount).To(Equal(count))
				Expect(next.TotalSize).To(Equal(size))
				Expect(next.Validate()).To(Succeed())
			})

			It("should preserve the resource reference", func() {
				ref, digest, rev, format, count, size := validResolveArgs()
				next := pending.Resolved(ref, digest, rev, format, count, size)
				Expect(next.Resource()).To(Equal(mc))
			})
		})

		Describe("CacheHit (Pending → Ready)", func() {
			It("should return a Ready state with all fields populated", func() {
				ref, digest, rev, format, count, size := validResolveArgs()
				next := pending.CacheHit(ref, digest, rev, format, count, size)

				Expect(next.Phase()).To(Equal(PhaseReady))
				Expect(next.ResolvedRef).To(Equal(ref))
				Expect(next.Digest).To(Equal(digest))
				Expect(next.ResolvedRevision).To(Equal(rev))
				Expect(next.Validate()).To(Succeed())
			})

			It("should preserve the resource reference", func() {
				ref, digest, rev, format, count, size := validResolveArgs()
				next := pending.CacheHit(ref, digest, rev, format, count, size)
				Expect(next.Resource()).To(Equal(mc))
			})
		})

		Describe("MarkFailed (Pending → Failed)", func() {
			DescribeTable("should create Failed state with correct fields",
				func(errMsg string, permanent bool, lastState string) {
					next := pending.MarkFailed(errMsg, permanent, lastState)

					Expect(next.Phase()).To(Equal(PhaseFailed))
					Expect(next.ErrorMessage).To(Equal(errMsg))
					Expect(next.Permanent).To(Equal(permanent))
					Expect(next.LastState).To(Equal(lastState))
					Expect(next.Validate()).To(Succeed())
				},
				Entry("permanent error from Pending", "repo not found", true, PhasePending),
				Entry("transient error from Pending", "network timeout", false, PhasePending),
			)

			It("should preserve the resource reference", func() {
				next := pending.MarkFailed("err", false, PhasePending)
				Expect(next.Resource()).To(Equal(mc))
			})
		})
	})

	Describe("from Resolving", func() {
		var resolving ModelCacheResolving
		var mc *v1alpha1.ModelCache

		BeforeEach(func() {
			mc = newModelCache(PhaseResolving)
			ref, digest, rev, format, count, size := validResolveArgs()
			resolving = ModelCacheResolving{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      ref,
					Digest:           digest,
					ResolvedRevision: rev,
					Format:           format,
					FileCount:        count,
					TotalSize:        size,
				},
			}
		})

		Describe("JobCreated (Resolving → Syncing)", func() {
			It("should return a Syncing state with SyncJobName and forwarded ResolveResult", func() {
				next := resolving.JobCreated("sync-job-abc123")

				Expect(next.Phase()).To(Equal(PhaseSyncing))
				Expect(next.SyncJobName).To(Equal("sync-job-abc123"))
				Expect(next.ResolvedRef).To(Equal(resolving.ResolvedRef))
				Expect(next.ResolvedRevision).To(Equal(resolving.ResolvedRevision))
				Expect(next.Format).To(Equal(resolving.Format))
				Expect(next.FileCount).To(Equal(resolving.FileCount))
				Expect(next.TotalSize).To(Equal(resolving.TotalSize))
				Expect(next.Validate()).To(Succeed())
			})

			It("should preserve the resource reference", func() {
				next := resolving.JobCreated("job-123")
				Expect(next.Resource()).To(Equal(mc))
			})
		})

		Describe("MarkFailed (Resolving → Failed)", func() {
			It("should transition to Failed with transient error", func() {
				next := resolving.MarkFailed("network error", false, PhaseResolving)

				Expect(next.Phase()).To(Equal(PhaseFailed))
				Expect(next.ErrorMessage).To(Equal("network error"))
				Expect(next.Permanent).To(BeFalse())
				Expect(next.LastState).To(Equal(PhaseResolving))
				Expect(next.Validate()).To(Succeed())
			})

			It("should transition to Failed with permanent error", func() {
				next := resolving.MarkFailed("invalid repo format", true, PhaseResolving)

				Expect(next.Phase()).To(Equal(PhaseFailed))
				Expect(next.Permanent).To(BeTrue())
			})
		})
	})

	Describe("from Syncing", func() {
		var syncing ModelCacheSyncing
		var mc *v1alpha1.ModelCache

		BeforeEach(func() {
			mc = newModelCache(PhaseSyncing)
			syncing = ModelCacheSyncing{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
					Digest:           "sha256:old",
					ResolvedRevision: "main",
					Format:           "safetensors",
					FileCount:        3,
					TotalSize:        1024,
				},
				SyncJob: SyncJob{SyncJobName: "sync-job-456"},
			}
		})

		Describe("SyncComplete (Syncing → Ready)", func() {
			It("should return a Ready state with new digest and resolve info", func() {
				newRef := "ghcr.io/jomcgi/models/llama:main"
				newDigest := "sha256:newdigest789"
				next := syncing.SyncComplete(newRef, newDigest, "main", "safetensors", 5, 2048)

				Expect(next.Phase()).To(Equal(PhaseReady))
				Expect(next.Digest).To(Equal(newDigest))
				Expect(next.ResolvedRef).To(Equal(newRef))
				Expect(next.FileCount).To(Equal(5))
				Expect(next.TotalSize).To(Equal(int64(2048)))
				Expect(next.Validate()).To(Succeed())
			})

			It("should preserve the resource reference", func() {
				next := syncing.SyncComplete("ref", "sha256:d", "rev", "gguf", 1, 100)
				Expect(next.Resource()).To(Equal(mc))
			})
		})

		Describe("MarkFailed (Syncing → Failed)", func() {
			It("should transition to Failed preserving error info", func() {
				next := syncing.MarkFailed("job failed: OOM", false, PhaseSyncing)

				Expect(next.Phase()).To(Equal(PhaseFailed))
				Expect(next.ErrorMessage).To(Equal("job failed: OOM"))
				Expect(next.Permanent).To(BeFalse())
				Expect(next.LastState).To(Equal(PhaseSyncing))
				Expect(next.Validate()).To(Succeed())
			})
		})
	})

	Describe("from Ready", func() {
		var ready ModelCacheReady
		var mc *v1alpha1.ModelCache

		BeforeEach(func() {
			mc = newModelCache(PhaseReady)
			ready = ModelCacheReady{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
					Digest:           "sha256:abc",
					ResolvedRevision: "main",
					Format:           "safetensors",
				},
			}
		})

		Describe("Resync (Ready → Pending)", func() {
			It("should return a Pending state", func() {
				next := ready.Resync()
				Expect(next.Phase()).To(Equal(PhasePending))
			})

			It("should preserve the resource reference", func() {
				next := ready.Resync()
				Expect(next.Resource()).To(Equal(mc))
			})
		})
	})

	Describe("from Failed", func() {
		Describe("Retry (Failed → Pending)", func() {
			It("should return a Pending state when Permanent is false", func() {
				mc := newModelCache(PhaseFailed)
				failed := ModelCacheFailed{
					resource:  mc,
					ErrorInfo: ErrorInfo{Permanent: false, LastState: PhasePending, ErrorMessage: "transient"},
				}
				next := failed.Retry()
				Expect(next).NotTo(BeNil())
				Expect(next.Phase()).To(Equal(PhasePending))
				Expect(next.Resource()).To(Equal(mc))
			})

			It("should return nil when Permanent is true (guard blocks retry)", func() {
				mc := newModelCache(PhaseFailed)
				failed := ModelCacheFailed{
					resource:  mc,
					ErrorInfo: ErrorInfo{Permanent: true, LastState: PhasePending, ErrorMessage: "permanent"},
				}
				next := failed.Retry()
				Expect(next).To(BeNil())
			})

			DescribeTable("guard behavior by Permanent flag",
				func(permanent bool, expectNil bool) {
					mc := newModelCache(PhaseFailed)
					failed := ModelCacheFailed{
						resource:  mc,
						ErrorInfo: ErrorInfo{Permanent: permanent, LastState: "Pending", ErrorMessage: "err"},
					}
					result := failed.Retry()
					if expectNil {
						Expect(result).To(BeNil())
					} else {
						Expect(result).NotTo(BeNil())
					}
				},
				Entry("retryable (Permanent=false)", false, false),
				Entry("not retryable (Permanent=true)", true, true),
			)
		})

		Describe("IsRetryable and RetryBackoff", func() {
			It("IsRetryable should always return true for Failed", func() {
				mc := newModelCache(PhaseFailed)
				failed := ModelCacheFailed{
					resource:  mc,
					ErrorInfo: ErrorInfo{Permanent: false, LastState: "Pending", ErrorMessage: "err"},
				}
				Expect(failed.IsRetryable()).To(BeTrue())
			})

			It("RetryBackoff should return the same as RequeueAfter", func() {
				mc := newModelCache(PhaseFailed)
				failed := ModelCacheFailed{
					resource:  mc,
					ErrorInfo: ErrorInfo{Permanent: false, LastState: "Pending", ErrorMessage: "err"},
				}
				Expect(failed.RetryBackoff()).To(Equal(failed.RequeueAfter()))
			})
		})
	})

	Describe("from Unknown", func() {
		Describe("Reset (Unknown → Pending)", func() {
			It("should return a Pending state for recovery", func() {
				mc := newModelCache("garbage-phase")
				unknown := ModelCacheUnknown{resource: mc, ObservedPhase: "garbage-phase"}
				next := unknown.Reset()

				Expect(next.Phase()).To(Equal(PhasePending))
				Expect(next.Resource()).To(Equal(mc))
			})
		})

		Describe("IsRetryable and RetryBackoff", func() {
			It("IsRetryable should always return true for Unknown", func() {
				mc := newModelCache("bad")
				unknown := ModelCacheUnknown{resource: mc, ObservedPhase: "bad"}
				Expect(unknown.IsRetryable()).To(BeTrue())
			})

			It("RetryBackoff should return a positive duration", func() {
				mc := newModelCache("bad")
				unknown := ModelCacheUnknown{resource: mc, ObservedPhase: "bad"}
				Expect(unknown.RetryBackoff()).To(BeNumerically(">", 0))
			})
		})
	})
})

// =============================================================================
// Calculator
// =============================================================================

var _ = Describe("ModelCacheCalculator", func() {
	var calc *ModelCacheCalculator

	BeforeEach(func() {
		calc = NewModelCacheCalculator(logr.Discard())
	})

	Describe("NewModelCacheCalculator", func() {
		It("should create a non-nil calculator", func() {
			Expect(calc).NotTo(BeNil())
		})
	})

	Describe("Calculate - empty and normal phases", func() {
		DescribeTable("should return the correct state type for valid phases",
			func(phase string, expectedType interface{}) {
				mc := newModelCacheWithStatus(validStatusForPhase(phase))
				state := calc.Calculate(mc)
				Expect(state).To(BeAssignableToTypeOf(expectedType))
			},
			Entry("empty phase → Pending", "", ModelCachePending{}),
			Entry("Pending phase → Pending", PhasePending, ModelCachePending{}),
			Entry("Resolving phase (valid) → Resolving", PhaseResolving, ModelCacheResolving{}),
			Entry("Syncing phase (valid) → Syncing", PhaseSyncing, ModelCacheSyncing{}),
			Entry("Ready phase (valid) → Ready", PhaseReady, ModelCacheReady{}),
			Entry("Failed phase (valid) → Failed", PhaseFailed, ModelCacheFailed{}),
			Entry("Unknown phase (valid) → Unknown", PhaseUnknown, ModelCacheUnknown{}),
		)
	})

	Describe("Calculate - unrecognized phase", func() {
		It("should return Unknown for completely invalid phase strings", func() {
			mc := newModelCache("SomeInvalidPhase")
			state := calc.Calculate(mc)

			unknown, ok := state.(ModelCacheUnknown)
			Expect(ok).To(BeTrue(), "expected ModelCacheUnknown, got %T", state)
			Expect(unknown.ObservedPhase).To(Equal("SomeInvalidPhase"))
		})

		DescribeTable("should return Unknown for any unrecognized phase",
			func(phase string) {
				mc := newModelCache(phase)
				state := calc.Calculate(mc)
				Expect(state).To(BeAssignableToTypeOf(ModelCacheUnknown{}))
			},
			Entry("partial phase name", "Pend"),
			Entry("lowercase phase", "resolving"),
			Entry("garbled value", "abc-xyz-123"),
			Entry("extra whitespace", " Pending"),
		)
	})

	Describe("Calculate - validation fallback to Unknown", func() {
		DescribeTable("should fall back to Unknown when required status fields are missing",
			func(phase string, mc *v1alpha1.ModelCache) {
				state := calc.Calculate(mc)
				unknown, ok := state.(ModelCacheUnknown)
				Expect(ok).To(BeTrue(), "expected Unknown for phase %q with invalid status, got %T", phase, state)
				Expect(unknown.ObservedPhase).To(Equal(phase))
			},
			Entry("Resolving without ResolvedRef",
				PhaseResolving,
				newModelCache(PhaseResolving), // no status fields set
			),
			Entry("Resolving without ResolvedRevision",
				PhaseResolving,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:       PhaseResolving,
					ResolvedRef: "ref",
					// ResolvedRevision missing
					Format: "gguf",
				}),
			),
			Entry("Resolving without Format",
				PhaseResolving,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:            PhaseResolving,
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					// Format missing
				}),
			),
			Entry("Syncing without SyncJobName",
				PhaseSyncing,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:            PhaseSyncing,
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "gguf",
					// SyncJobName missing
				}),
			),
			Entry("Ready without Digest",
				PhaseReady,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:            PhaseReady,
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "gguf",
					// Digest missing
				}),
			),
			Entry("Failed without ErrorMessage",
				PhaseFailed,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:     PhaseFailed,
					LastState: "Pending",
					// ErrorMessage missing
				}),
			),
			Entry("Failed without LastState",
				PhaseFailed,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase:        PhaseFailed,
					ErrorMessage: "something",
					// LastState missing
				}),
			),
			Entry("Unknown without ObservedPhase",
				PhaseUnknown,
				newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
					Phase: PhaseUnknown,
					// ObservedPhase missing - Validate() requires it to be non-empty
				}),
			),
		)
	})

	Describe("Calculate - data mapping from status", func() {
		It("should map all Resolving fields from status", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:            PhaseResolving,
				ResolvedRef:      "ghcr.io/jomcgi/models/llama:main",
				Digest:           "sha256:abc",
				ResolvedRevision: "main",
				Format:           "safetensors",
				FileCount:        5,
				TotalSize:        8192,
			})

			state := calc.Calculate(mc)
			resolving, ok := state.(ModelCacheResolving)
			Expect(ok).To(BeTrue())
			Expect(resolving.ResolvedRef).To(Equal("ghcr.io/jomcgi/models/llama:main"))
			Expect(resolving.Digest).To(Equal("sha256:abc"))
			Expect(resolving.ResolvedRevision).To(Equal("main"))
			Expect(resolving.Format).To(Equal("safetensors"))
			Expect(resolving.FileCount).To(Equal(5))
			Expect(resolving.TotalSize).To(Equal(int64(8192)))
		})

		It("should map all Syncing fields from status", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:            PhaseSyncing,
				ResolvedRef:      "ref",
				ResolvedRevision: "main",
				Format:           "gguf",
				SyncJobName:      "my-sync-job",
			})

			state := calc.Calculate(mc)
			syncing, ok := state.(ModelCacheSyncing)
			Expect(ok).To(BeTrue())
			Expect(syncing.SyncJobName).To(Equal("my-sync-job"))
			Expect(syncing.ResolvedRef).To(Equal("ref"))
		})

		It("should map all Ready fields from status", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:            PhaseReady,
				ResolvedRef:      "ref",
				Digest:           "sha256:readydigest",
				ResolvedRevision: "main",
				Format:           "safetensors",
				FileCount:        3,
				TotalSize:        4096,
			})

			state := calc.Calculate(mc)
			ready, ok := state.(ModelCacheReady)
			Expect(ok).To(BeTrue())
			Expect(ready.Digest).To(Equal("sha256:readydigest"))
			Expect(ready.FileCount).To(Equal(3))
			Expect(ready.TotalSize).To(Equal(int64(4096)))
		})

		It("should map all Failed fields from status", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:        PhaseFailed,
				ErrorMessage: "push failed",
				LastState:    PhaseSyncing,
				Permanent:    true,
			})

			state := calc.Calculate(mc)
			failed, ok := state.(ModelCacheFailed)
			Expect(ok).To(BeTrue())
			Expect(failed.ErrorMessage).To(Equal("push failed"))
			Expect(failed.LastState).To(Equal(PhaseSyncing))
			Expect(failed.Permanent).To(BeTrue())
		})

		It("should map ObservedPhase for Unknown state", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:         PhaseUnknown,
				ObservedPhase: "SomeOldPhase",
			})

			state := calc.Calculate(mc)
			unknown, ok := state.(ModelCacheUnknown)
			Expect(ok).To(BeTrue())
			Expect(unknown.ObservedPhase).To(Equal("SomeOldPhase"))
		})
	})

	Describe("Calculate - deletion timestamp", func() {
		It("should calculate normally when DeletionTimestamp is set (no deletion states)", func() {
			mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
				Phase:            PhaseReady,
				ResolvedRef:      "ref",
				Digest:           "sha256:abc",
				ResolvedRevision: "main",
				Format:           "safetensors",
			})
			now := metav1.Now()
			mc.DeletionTimestamp = &now

			// This operator has no deletion states, so it just calculates normally
			state := calc.Calculate(mc)
			Expect(state).To(BeAssignableToTypeOf(ModelCacheReady{}))
		})

		It("should fall back to Unknown for invalid status even with DeletionTimestamp", func() {
			mc := newModelCache(PhaseResolving)
			now := metav1.Now()
			mc.DeletionTimestamp = &now

			state := calc.Calculate(mc)
			Expect(state).To(BeAssignableToTypeOf(ModelCacheUnknown{}))
		})
	})
})

// validStatusForPhase returns a fully-populated status for a given phase.
func validStatusForPhase(phase string) v1alpha1.ModelCacheStatus {
	base := v1alpha1.ModelCacheStatus{Phase: phase}
	switch phase {
	case "", PhasePending:
		// no extra fields needed
	case PhaseResolving:
		base.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
		base.ResolvedRevision = "main"
		base.Format = "safetensors"
		base.Digest = "sha256:abc"
		base.FileCount = 3
		base.TotalSize = 1024
	case PhaseSyncing:
		base.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
		base.ResolvedRevision = "main"
		base.Format = "safetensors"
		base.SyncJobName = "sync-job-123"
	case PhaseReady:
		base.ResolvedRef = "ghcr.io/jomcgi/models/llama:main"
		base.Digest = "sha256:readydigest"
		base.ResolvedRevision = "main"
		base.Format = "safetensors"
		base.FileCount = 3
		base.TotalSize = 1024
	case PhaseFailed:
		base.ErrorMessage = "something went wrong"
		base.LastState = PhasePending
	case PhaseUnknown:
		base.ObservedPhase = "SomeOldPhase"
	}
	return base
}

// =============================================================================
// Visit pattern
// =============================================================================

var _ = Describe("Visit pattern", func() {
	var mc *v1alpha1.ModelCache

	BeforeEach(func() {
		mc = newModelCache("")
	})

	Describe("Visit with full visitor", func() {
		DescribeTable("should dispatch to correct handler",
			func(state ModelCacheState, expected string) {
				visitor := &ModelCacheFuncVisitor[string]{
					OnPending:   func(_ ModelCachePending) string { return "pending" },
					OnResolving: func(_ ModelCacheResolving) string { return "resolving" },
					OnSyncing:   func(_ ModelCacheSyncing) string { return "syncing" },
					OnReady:     func(_ ModelCacheReady) string { return "ready" },
					OnFailed:    func(_ ModelCacheFailed) string { return "failed" },
					OnUnknown:   func(_ ModelCacheUnknown) string { return "unknown" },
				}
				result := Visit(state, visitor)
				Expect(result).To(Equal(expected))
			},
			Entry("Pending state", ModelCachePending{resource: nil}, "pending"),
			Entry("Resolving state", ModelCacheResolving{resource: nil}, "resolving"),
			Entry("Syncing state", ModelCacheSyncing{resource: nil}, "syncing"),
			Entry("Ready state", ModelCacheReady{resource: nil}, "ready"),
			Entry("Failed state", ModelCacheFailed{resource: nil}, "failed"),
			Entry("Unknown state", ModelCacheUnknown{resource: nil, ObservedPhase: "x"}, "unknown"),
		)
	})

	Describe("ModelCacheFuncVisitor - Default fallback", func() {
		It("should call Default when specific handler is nil", func() {
			visitor := &ModelCacheFuncVisitor[string]{
				Default: func(s ModelCacheState) string { return "default:" + s.Phase() },
			}

			states := []ModelCacheState{
				ModelCachePending{resource: mc},
				ModelCacheResolving{resource: mc},
				ModelCacheSyncing{resource: mc},
				ModelCacheReady{resource: mc},
				ModelCacheFailed{resource: mc},
				ModelCacheUnknown{resource: mc, ObservedPhase: "x"},
			}

			for _, state := range states {
				result := Visit(state, visitor)
				Expect(result).To(Equal("default:" + state.Phase()))
			}
		})

		It("should prefer specific handler over Default", func() {
			visitor := &ModelCacheFuncVisitor[string]{
				OnPending: func(_ ModelCachePending) string { return "specific" },
				Default:   func(_ ModelCacheState) string { return "default" },
			}

			result := Visit(ModelCachePending{resource: mc}, visitor)
			Expect(result).To(Equal("specific"))

			// Other states use default
			result = Visit(ModelCacheResolving{resource: mc}, visitor)
			Expect(result).To(Equal("default"))
		})

		It("should return zero value when no handler and no Default", func() {
			visitor := &ModelCacheFuncVisitor[string]{}

			states := []ModelCacheState{
				ModelCachePending{resource: mc},
				ModelCacheResolving{resource: mc},
				ModelCacheSyncing{resource: mc},
				ModelCacheReady{resource: mc},
				ModelCacheFailed{resource: mc},
				ModelCacheUnknown{resource: mc, ObservedPhase: "x"},
			}

			for _, state := range states {
				result := Visit(state, visitor)
				Expect(result).To(Equal(""))
			}
		})

		It("should work with non-string return types", func() {
			visitor := &ModelCacheFuncVisitor[int]{
				OnPending:   func(_ ModelCachePending) int { return 1 },
				OnResolving: func(_ ModelCacheResolving) int { return 2 },
				Default:     func(_ ModelCacheState) int { return 99 },
			}

			Expect(Visit(ModelCachePending{resource: mc}, visitor)).To(Equal(1))
			Expect(Visit(ModelCacheResolving{resource: mc}, visitor)).To(Equal(2))
			Expect(Visit(ModelCacheSyncing{resource: mc}, visitor)).To(Equal(99))
		})
	})

	Describe("ModelCacheFuncVisitor - state access in handlers", func() {
		It("should receive correct state in handler", func() {
			ref, digest, rev, format, count, size := validResolveArgs()
			resolving := ModelCacheResolving{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      ref,
					Digest:           digest,
					ResolvedRevision: rev,
					Format:           format,
					FileCount:        count,
					TotalSize:        size,
				},
			}

			var capturedRef string
			visitor := &ModelCacheFuncVisitor[bool]{
				OnResolving: func(s ModelCacheResolving) bool {
					capturedRef = s.ResolvedRef
					return true
				},
			}

			result := Visit(resolving, visitor)
			Expect(result).To(BeTrue())
			Expect(capturedRef).To(Equal(ref))
		})
	})
})

// =============================================================================
// Status helpers
// =============================================================================

var _ = Describe("Status helpers", func() {
	Describe("HasSpecChanged", func() {
		DescribeTable("should correctly detect spec changes",
			func(generation, observedGeneration int64, expected bool) {
				mc := newModelCache(PhasePending)
				mc.Generation = generation
				mc.Status.ObservedGeneration = observedGeneration
				Expect(HasSpecChanged(mc)).To(Equal(expected))
			},
			Entry("changed: generation > observedGeneration", int64(3), int64(2), true),
			Entry("unchanged: equal generations", int64(3), int64(3), false),
			Entry("initial: both zero", int64(0), int64(0), false),
			Entry("new resource: generation=1, observed=0", int64(1), int64(0), true),
		)
	})

	Describe("UpdateObservedGeneration", func() {
		It("should update observedGeneration to current generation", func() {
			mc := newModelCache(PhasePending)
			mc.Generation = 7
			mc.Status.ObservedGeneration = 5

			updated := UpdateObservedGeneration(mc)
			Expect(updated.Status.ObservedGeneration).To(Equal(int64(7)))
		})

		It("should not mutate the original resource", func() {
			mc := newModelCache(PhasePending)
			mc.Generation = 7
			mc.Status.ObservedGeneration = 5

			_ = UpdateObservedGeneration(mc)
			Expect(mc.Status.ObservedGeneration).To(Equal(int64(5)))
		})

		It("should return a deep copy", func() {
			mc := newModelCache(PhasePending)
			mc.Generation = 2
			mc.Status.ObservedGeneration = 1

			updated := UpdateObservedGeneration(mc)
			Expect(updated).NotTo(BeIdenticalTo(mc))
		})
	})

	Describe("ApplyStatus", func() {
		It("should set Phase=Pending for Pending state without mutating original", func() {
			mc := newModelCache("")
			s := ModelCachePending{resource: mc}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhasePending))
			Expect(result).NotTo(BeIdenticalTo(mc))
		})

		It("should set Phase=Resolving and all resolve fields", func() {
			mc := newModelCache("")
			ref, digest, rev, format, count, size := validResolveArgs()
			s := ModelCacheResolving{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      ref,
					Digest:           digest,
					ResolvedRevision: rev,
					Format:           format,
					FileCount:        count,
					TotalSize:        size,
				},
			}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhaseResolving))
			Expect(result.Status.ResolvedRef).To(Equal(ref))
			Expect(result.Status.Digest).To(Equal(digest))
			Expect(result.Status.ResolvedRevision).To(Equal(rev))
			Expect(result.Status.Format).To(Equal(format))
			Expect(result.Status.FileCount).To(Equal(count))
			Expect(result.Status.TotalSize).To(Equal(size))
		})

		It("should set Phase=Syncing and all syncing fields", func() {
			mc := newModelCache("")
			s := ModelCacheSyncing{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					ResolvedRevision: "main",
					Format:           "gguf",
				},
				SyncJob: SyncJob{SyncJobName: "job-xyz"},
			}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhaseSyncing))
			Expect(result.Status.SyncJobName).To(Equal("job-xyz"))
			Expect(result.Status.ResolvedRef).To(Equal("ref"))
		})

		It("should set Phase=Ready and all ready fields", func() {
			mc := newModelCache("")
			s := ModelCacheReady{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					Digest:           "sha256:final",
					ResolvedRevision: "main",
					Format:           "safetensors",
					FileCount:        4,
					TotalSize:        8192,
				},
			}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhaseReady))
			Expect(result.Status.Digest).To(Equal("sha256:final"))
			Expect(result.Status.FileCount).To(Equal(4))
			Expect(result.Status.TotalSize).To(Equal(int64(8192)))
		})

		It("should set Phase=Failed and all error fields", func() {
			mc := newModelCache("")
			s := ModelCacheFailed{
				resource: mc,
				ErrorInfo: ErrorInfo{
					Permanent:    true,
					LastState:    PhaseResolving,
					ErrorMessage: "network unreachable",
				},
			}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhaseFailed))
			Expect(result.Status.Permanent).To(BeTrue())
			Expect(result.Status.LastState).To(Equal(PhaseResolving))
			Expect(result.Status.ErrorMessage).To(Equal("network unreachable"))
		})

		It("should set Phase=Unknown and ObservedPhase", func() {
			mc := newModelCache("")
			s := ModelCacheUnknown{resource: mc, ObservedPhase: "WeirdPhase"}

			result := s.ApplyStatus()
			Expect(result.Status.Phase).To(Equal(PhaseUnknown))
			Expect(result.Status.ObservedPhase).To(Equal("WeirdPhase"))
		})

		It("should not mutate the original resource for any state", func() {
			mc := newModelCache("")
			originalPhase := mc.Status.Phase

			states := []interface {
				ApplyStatus() *v1alpha1.ModelCache
			}{
				ModelCachePending{resource: mc},
				ModelCacheResolving{resource: mc, ResolveResult: ResolveResult{ResolvedRef: "r", ResolvedRevision: "v", Format: "f"}},
				ModelCacheSyncing{resource: mc, ResolveResult: ResolveResult{ResolvedRef: "r", ResolvedRevision: "v", Format: "f"}, SyncJob: SyncJob{SyncJobName: "j"}},
				ModelCacheReady{resource: mc, ResolveResult: ResolveResult{ResolvedRef: "r", Digest: "d", ResolvedRevision: "v", Format: "f"}},
				ModelCacheFailed{resource: mc, ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "err"}},
				ModelCacheUnknown{resource: mc, ObservedPhase: "x"},
			}

			for _, s := range states {
				result := s.ApplyStatus()
				Expect(mc.Status.Phase).To(Equal(originalPhase), "original should not be mutated")
				Expect(result).NotTo(BeIdenticalTo(mc), "should return a copy")
			}
		})
	})

	Describe("SSAPatch", func() {
		It("should produce a non-nil patch for Pending state", func() {
			mc := newModelCache(PhasePending)
			s := ModelCachePending{resource: mc}

			patch, err := SSAPatch(s)
			Expect(err).NotTo(HaveOccurred())
			Expect(patch).NotTo(BeNil())
		})

		It("should produce a non-nil patch for Ready state", func() {
			mc := newModelCache(PhaseReady)
			s := ModelCacheReady{
				resource: mc,
				ResolveResult: ResolveResult{
					ResolvedRef:      "ref",
					Digest:           "sha256:abc",
					ResolvedRevision: "main",
					Format:           "safetensors",
				},
			}

			patch, err := SSAPatch(s)
			Expect(err).NotTo(HaveOccurred())
			Expect(patch).NotTo(BeNil())
		})

		It("should produce a non-nil patch for Failed state", func() {
			mc := newModelCache(PhaseFailed)
			s := ModelCacheFailed{
				resource:  mc,
				ErrorInfo: ErrorInfo{LastState: "Pending", ErrorMessage: "err"},
			}

			patch, err := SSAPatch(s)
			Expect(err).NotTo(HaveOccurred())
			Expect(patch).NotTo(BeNil())
		})

		It("should not mutate the original resource", func() {
			mc := newModelCache(PhasePending)
			mc.Spec.Repo = "some-repo"
			originalSpec := mc.Spec

			s := ModelCachePending{resource: mc}
			_, err := SSAPatch(s)
			Expect(err).NotTo(HaveOccurred())
			Expect(mc.Spec).To(Equal(originalSpec))
		})
	})

	Describe("FieldManager", func() {
		It("should be set to the expected controller name", func() {
			Expect(FieldManager).To(Equal("modelcache-controller"))
		})
	})
})

// =============================================================================
// Integration tests - full lifecycle paths
// =============================================================================

var _ = Describe("Integration", func() {
	var calc *ModelCacheCalculator

	BeforeEach(func() {
		calc = NewModelCacheCalculator(logr.Discard())
	})

	It("happy path: Pending → Resolving → Syncing → Ready", func() {
		mc := newModelCache("")

		// Start in Pending
		state := calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCachePending{}))

		// Transition to Resolving
		pending := state.(ModelCachePending)
		resolving := pending.Resolved("ghcr.io/m/llama:main", "sha256:init", "main", "safetensors", 3, 1024)
		Expect(resolving.Phase()).To(Equal(PhaseResolving))
		Expect(resolving.Validate()).To(Succeed())

		// Persist Resolving → recalculate
		mc = resolving.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheResolving{}))

		// Transition to Syncing
		resolving = state.(ModelCacheResolving)
		syncing := resolving.JobCreated("sync-job-789")
		Expect(syncing.Phase()).To(Equal(PhaseSyncing))
		Expect(syncing.Validate()).To(Succeed())

		// Persist Syncing → recalculate
		mc = syncing.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheSyncing{}))

		// Transition to Ready
		syncing = state.(ModelCacheSyncing)
		ready := syncing.SyncComplete("ghcr.io/m/llama:main", "sha256:final", "main", "safetensors", 3, 1024)
		Expect(ready.Phase()).To(Equal(PhaseReady))
		Expect(ready.Validate()).To(Succeed())

		// Persist Ready → recalculate
		mc = ready.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheReady{}))
	})

	It("cache hit path: Pending → Ready", func() {
		mc := newModelCache("")

		state := calc.Calculate(mc)
		pending := state.(ModelCachePending)
		ready := pending.CacheHit("ghcr.io/m/llama:main", "sha256:cached", "main", "safetensors", 3, 1024)

		Expect(ready.Phase()).To(Equal(PhaseReady))
		Expect(ready.Validate()).To(Succeed())

		mc = ready.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheReady{}))
	})

	It("retry path: Pending → Failed (transient) → Pending", func() {
		mc := newModelCache("")

		state := calc.Calculate(mc)
		pending := state.(ModelCachePending)
		failed := pending.MarkFailed("transient network error", false, PhasePending)
		Expect(failed.Phase()).To(Equal(PhaseFailed))

		// Persist Failed → recalculate
		mc = failed.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheFailed{}))

		// Retry
		failed = state.(ModelCacheFailed)
		retried := failed.Retry()
		Expect(retried).NotTo(BeNil())
		Expect(retried.Phase()).To(Equal(PhasePending))
	})

	It("permanent failure path: Pending → Failed (permanent) — no retry", func() {
		mc := newModelCache("")

		state := calc.Calculate(mc)
		pending := state.(ModelCachePending)
		failed := pending.MarkFailed("invalid repo", true, PhasePending)

		mc = failed.ApplyStatus()
		state = calc.Calculate(mc)
		failedState := state.(ModelCacheFailed)

		Expect(failedState.Retry()).To(BeNil())
	})

	It("resync path: Ready → Pending", func() {
		mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
			Phase:            PhaseReady,
			ResolvedRef:      "ref",
			Digest:           "sha256:abc",
			ResolvedRevision: "main",
			Format:           "safetensors",
		})

		state := calc.Calculate(mc)
		ready := state.(ModelCacheReady)
		pending := ready.Resync()

		Expect(pending.Phase()).To(Equal(PhasePending))
		mc = pending.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCachePending{}))
	})

	It("recovery path: Unknown → Pending", func() {
		mc := newModelCache("WeirdOldPhase")
		state := calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCacheUnknown{}))

		unknown := state.(ModelCacheUnknown)
		pending := unknown.Reset()
		Expect(pending.Phase()).To(Equal(PhasePending))

		mc = pending.ApplyStatus()
		state = calc.Calculate(mc)
		Expect(state).To(BeAssignableToTypeOf(ModelCachePending{}))
	})

	It("syncing failure path: Resolving → Syncing → Failed (transient) → Pending", func() {
		mc := newModelCacheWithStatus(v1alpha1.ModelCacheStatus{
			Phase:            PhaseSyncing,
			ResolvedRef:      "ref",
			ResolvedRevision: "main",
			Format:           "safetensors",
			SyncJobName:      "sync-job-abc",
		})

		state := calc.Calculate(mc)
		syncing := state.(ModelCacheSyncing)
		failed := syncing.MarkFailed("job OOM killed", false, PhaseSyncing)

		mc = failed.ApplyStatus()
		state = calc.Calculate(mc)
		failedState := state.(ModelCacheFailed)

		retried := failedState.Retry()
		Expect(retried).NotTo(BeNil())
		Expect(retried.Phase()).To(Equal(PhasePending))
	})
})
