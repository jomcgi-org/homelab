package statemachine_test

// Drift guard for generated code. The model_cache_*.go files are generated
// by sextant from modelcache.sextant.yaml; hand-editing generated output
// kills the generator — the next regeneration silently reverts the edit, so
// nobody regenerates and drift compounds behind "DO NOT EDIT" headers. This
// test regenerates from the spec and fails on any byte difference.
//
// To change generated behavior: edit the spec (or sextant's templates), then
// `go generate ./projects/operators/oci-model-cache/internal/statemachine`.

import (
	_ "embed"
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/codegen"
	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

//go:embed modelcache.sextant.yaml
var specYAML []byte

var committed = map[string][]byte{
	"model_cache_calculator.go":    committedCalculator,
	"model_cache_metrics.go":       committedMetrics,
	"model_cache_observability.go": committedObservability,
	"model_cache_phases.go":        committedPhases,
	"model_cache_status.go":        committedStatus,
	"model_cache_transitions.go":   committedTransitions,
	"model_cache_types.go":         committedTypes,
	"model_cache_visit.go":         committedVisit,
}

//go:embed model_cache_calculator.go
var committedCalculator []byte

//go:embed model_cache_metrics.go
var committedMetrics []byte

//go:embed model_cache_observability.go
var committedObservability []byte

//go:embed model_cache_phases.go
var committedPhases []byte

//go:embed model_cache_status.go
var committedStatus []byte

//go:embed model_cache_transitions.go
var committedTransitions []byte

//go:embed model_cache_types.go
var committedTypes []byte

//go:embed model_cache_visit.go
var committedVisit []byte

func TestGeneratedCodeMatchesSpec(t *testing.T) {
	sm, err := schema.Parse(specYAML)
	if err != nil {
		t.Fatalf("parse spec: %v", err)
	}
	if err := schema.Validate(sm); err != nil {
		t.Fatalf("validate spec: %v", err)
	}

	tmpDir := t.TempDir()
	gen, err := codegen.New(codegen.Config{
		OutputDir:     tmpDir,
		Package:       "statemachine",
		Module:        "github.com/jomcgi/homelab",
		APIImportPath: "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1",
	})
	if err != nil {
		t.Fatalf("create generator: %v", err)
	}
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("generate: %v", err)
	}

	regenerated, err := os.ReadDir(tmpDir)
	if err != nil {
		t.Fatalf("read output dir: %v", err)
	}

	seen := make(map[string]bool)
	for _, entry := range regenerated {
		name := entry.Name()
		seen[name] = true

		want, ok := committed[name]
		if !ok {
			t.Errorf("%s: generated but not committed — run `go generate` on this package, commit the result, and add the file to this test's embed list", name)
			continue
		}
		got, err := os.ReadFile(filepath.Join(tmpDir, name))
		if err != nil {
			t.Fatalf("read regenerated %s: %v", name, err)
		}
		if string(got) != string(want) {
			t.Errorf("%s: committed file differs from regenerated output — run `go generate` on this package and commit the result (or fix the spec/templates)", name)
		}
	}

	for name := range committed {
		if !seen[name] {
			t.Errorf("%s: committed but no longer generated — delete it and remove it from this test's embed list", name)
		}
	}
}
