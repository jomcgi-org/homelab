/*
Copyright 2025.

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

// modelcache_types_test.go provides additional coverage for ModelCache CRD
// types beyond the JSON/DeepCopy/scheme tests in types_test.go.
//
// Focus areas:
//   - Numeric zero-values in ModelCacheStatus (TotalSize, FileCount)
//   - Permanent bool field round-trip
//   - Spec field boundary cases (empty vs non-empty optional fields)
//   - Deep copy independence for status Conditions slice
//   - ModelCacheSpec TTL nil vs zero-value Duration

package v1alpha1

import (
	"encoding/json"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TestModelCacheStatusNumericZeroValues verifies that zero numeric fields in
// ModelCacheStatus are omitted from JSON (all have omitempty) and that
// non-zero values round-trip correctly.
func TestModelCacheStatusNumericZeroValues(t *testing.T) {
	t.Run("zero TotalSize and FileCount are omitted", func(t *testing.T) {
		status := ModelCacheStatus{
			Phase:     "Syncing",
			TotalSize: 0,
			FileCount: 0,
		}
		data, err := json.Marshal(status)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if _, ok := raw["totalSize"]; ok {
			t.Error("totalSize should be omitted when zero")
		}
		if _, ok := raw["fileCount"]; ok {
			t.Error("fileCount should be omitted when zero")
		}
	})

	t.Run("non-zero TotalSize and FileCount are preserved", func(t *testing.T) {
		status := ModelCacheStatus{
			Phase:     "Ready",
			TotalSize: 4_294_967_296, // 4 GiB
			FileCount: 7,
		}
		data, err := json.Marshal(status)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var got ModelCacheStatus
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if got.TotalSize != 4_294_967_296 {
			t.Errorf("TotalSize: want 4294967296, got %d", got.TotalSize)
		}
		if got.FileCount != 7 {
			t.Errorf("FileCount: want 7, got %d", got.FileCount)
		}
	})
}

// TestModelCacheStatusPermanentField verifies the Permanent bool field is
// included when true and omitted (omitempty) when false.
func TestModelCacheStatusPermanentField(t *testing.T) {
	t.Run("Permanent=true is included in JSON", func(t *testing.T) {
		status := ModelCacheStatus{Permanent: true}
		data, err := json.Marshal(status)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		v, ok := raw["permanent"]
		if !ok {
			t.Fatal("permanent key should be present when true")
		}
		if v != true {
			t.Errorf("permanent: want true, got %v", v)
		}
	})

	t.Run("Permanent=false is omitted from JSON", func(t *testing.T) {
		status := ModelCacheStatus{Permanent: false}
		data, err := json.Marshal(status)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if _, ok := raw["permanent"]; ok {
			t.Error("permanent should be omitted when false (omitempty)")
		}
	})

	t.Run("Permanent round-trips correctly", func(t *testing.T) {
		status := ModelCacheStatus{
			Phase:        "Failed",
			Permanent:    true,
			LastState:    "Syncing",
			ErrorMessage: "fatal error",
		}
		data, err := json.Marshal(status)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var got ModelCacheStatus
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if !got.Permanent {
			t.Error("Permanent should be true after round-trip")
		}
	})
}

// TestModelCacheSpecOptionalFieldsDefaultBehavior verifies that optional
// Spec fields behave correctly with their zero values.
func TestModelCacheSpecOptionalFieldsDefaultBehavior(t *testing.T) {
	t.Run("empty File field is omitted", func(t *testing.T) {
		spec := ModelCacheSpec{
			Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
			Registry: "ghcr.io/jomcgi/models",
			File:     "",
		}
		data, err := json.Marshal(spec)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if _, ok := raw["file"]; ok {
			t.Error("file should be omitted when empty (omitempty)")
		}
	})

	t.Run("empty Tag field is omitted", func(t *testing.T) {
		spec := ModelCacheSpec{
			Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
			Registry: "ghcr.io/jomcgi/models",
			Tag:      "",
		}
		data, err := json.Marshal(spec)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if _, ok := raw["tag"]; ok {
			t.Error("tag should be omitted when empty (omitempty)")
		}
	})

	t.Run("empty ModelDir field is omitted", func(t *testing.T) {
		spec := ModelCacheSpec{
			Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
			Registry: "ghcr.io/jomcgi/models",
			ModelDir: "",
		}
		data, err := json.Marshal(spec)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		if _, ok := raw["modelDir"]; ok {
			t.Error("modelDir should be omitted when empty (omitempty)")
		}
	})

	t.Run("non-empty File, Tag, ModelDir are included", func(t *testing.T) {
		spec := ModelCacheSpec{
			Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
			Registry: "ghcr.io/jomcgi/models",
			File:     "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
			Tag:      "v1.0",
			ModelDir: "/models/llama",
		}
		data, err := json.Marshal(spec)
		if err != nil {
			t.Fatalf("Marshal error: %v", err)
		}
		var raw map[string]interface{}
		if err := json.Unmarshal(data, &raw); err != nil {
			t.Fatalf("Unmarshal error: %v", err)
		}
		for _, key := range []string{"file", "tag", "modelDir"} {
			if _, ok := raw[key]; !ok {
				t.Errorf("expected key %q to be present", key)
			}
		}
	})
}

// TestModelCacheStatusConditionsDeepCopy verifies that Conditions slice
// is properly deep-copied and mutation of the copy does not affect the original.
func TestModelCacheStatusConditionsDeepCopy(t *testing.T) {
	fixedTime := metav1.NewTime(time.Date(2025, 6, 1, 12, 0, 0, 0, time.UTC))
	original := &ModelCacheStatus{
		Phase: "Ready",
		Conditions: []metav1.Condition{
			{
				Type:               "Ready",
				Status:             metav1.ConditionTrue,
				ObservedGeneration: 2,
				LastTransitionTime: fixedTime,
				Reason:             "Synced",
				Message:            "Model cached successfully",
			},
		},
	}

	copy := original.DeepCopy()
	if copy == nil {
		t.Fatal("DeepCopy returned nil")
	}

	// Mutate the copy
	copy.Conditions[0].Status = metav1.ConditionFalse
	copy.Conditions[0].Message = "mutated"

	// Original must be unchanged
	if original.Conditions[0].Status != metav1.ConditionTrue {
		t.Errorf("original Conditions[0].Status mutated: want %q, got %q",
			metav1.ConditionTrue, original.Conditions[0].Status)
	}
	if original.Conditions[0].Message != "Model cached successfully" {
		t.Errorf("original Conditions[0].Message mutated: want %q, got %q",
			"Model cached successfully", original.Conditions[0].Message)
	}
}

// TestModelCacheSpecTTLBoundaryValues verifies that TTL handles boundary Duration values.
func TestModelCacheSpecTTLBoundaryValues(t *testing.T) {
	cases := []struct {
		name string
		ttl  *metav1.Duration
	}{
		{
			name: "TTL of 1 nanosecond",
			ttl:  &metav1.Duration{Duration: 1},
		},
		{
			name: "TTL of 24 hours",
			ttl:  &metav1.Duration{Duration: 24 * time.Hour},
		},
		{
			name: "TTL of 8760 hours (1 year)",
			ttl:  &metav1.Duration{Duration: 8760 * time.Hour},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spec := ModelCacheSpec{
				Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
				Registry: "ghcr.io/jomcgi/models",
				TTL:      tc.ttl,
			}
			data, err := json.Marshal(spec)
			if err != nil {
				t.Fatalf("Marshal error: %v", err)
			}
			var got ModelCacheSpec
			if err := json.Unmarshal(data, &got); err != nil {
				t.Fatalf("Unmarshal error: %v", err)
			}
			if got.TTL == nil {
				t.Fatal("TTL should not be nil after round-trip")
			}
			if got.TTL.Duration != tc.ttl.Duration {
				t.Errorf("TTL.Duration: want %v, got %v", tc.ttl.Duration, got.TTL.Duration)
			}
		})
	}
}

// TestModelCacheStatusObservedGenerationZero verifies that ObservedGeneration=0
// is omitted from JSON (omitempty).
func TestModelCacheStatusObservedGenerationZero(t *testing.T) {
	status := ModelCacheStatus{
		Phase:              "Pending",
		ObservedGeneration: 0,
	}
	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("Marshal error: %v", err)
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("Unmarshal error: %v", err)
	}
	if _, ok := raw["observedGeneration"]; ok {
		t.Error("observedGeneration should be omitted when zero (omitempty)")
	}
}

// TestModelCacheStatusObservedGenerationNonZero verifies that non-zero
// ObservedGeneration round-trips correctly.
func TestModelCacheStatusObservedGenerationNonZero(t *testing.T) {
	status := ModelCacheStatus{
		Phase:              "Ready",
		ObservedGeneration: 42,
	}
	data, err := json.Marshal(status)
	if err != nil {
		t.Fatalf("Marshal error: %v", err)
	}
	var got ModelCacheStatus
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("Unmarshal error: %v", err)
	}
	if got.ObservedGeneration != 42 {
		t.Errorf("ObservedGeneration: want 42, got %d", got.ObservedGeneration)
	}
}

// TestModelCacheSpecDeepCopyTTL verifies TTL pointer is deep-copied correctly.
func TestModelCacheSpecDeepCopyTTL(t *testing.T) {
	original := &ModelCacheSpec{
		Repo:     "bartowski/Llama-3.2-1B-Instruct-GGUF",
		Registry: "ghcr.io/jomcgi/models",
		TTL:      &metav1.Duration{Duration: 24 * time.Hour},
	}

	copy := original.DeepCopy()
	if copy == nil {
		t.Fatal("DeepCopy returned nil")
	}

	// Mutate the copy's TTL
	copy.TTL.Duration = 48 * time.Hour

	// Original should be unchanged
	if original.TTL.Duration != 24*time.Hour {
		t.Errorf("original TTL.Duration mutated: want 24h, got %v", original.TTL.Duration)
	}
}
