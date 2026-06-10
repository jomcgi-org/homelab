package codegen_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/codegen"
	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// TestGenerator_NoUnformattedFilesOnSuccess verifies that no .unformatted debug
// files remain in the output directory after a successful code generation run.
func TestGenerator_NoUnformattedFilesOnSuccess(t *testing.T) {
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{
			PhaseField: "phase",
		},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-coverage-*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	gen, err := codegen.New(codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	})
	if err != nil {
		t.Fatalf("failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Scan the output directory for any .unformatted debug files.
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		t.Fatalf("failed to read output dir: %v", err)
	}

	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".unformatted") {
			t.Errorf("unexpected .unformatted debug file left behind: %s",
				filepath.Join(tmpDir, entry.Name()))
		}
	}
}
