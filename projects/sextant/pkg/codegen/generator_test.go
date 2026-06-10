package codegen_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/sextant/pkg/codegen"
	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

func TestGenerator_NoErrorHandling(t *testing.T) {
	// Without an errorHandling block, retry helpers are requeue-based:
	// no retry-count exponential backoff, no jitter machinery.
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
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
	}

	// Create temp output directory
	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated transitions file
	transitionsPath := filepath.Join(tmpDir, "test_resource_transitions.go")
	content, err := os.ReadFile(transitionsPath)
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	contentStr := string(content)

	// No retry-count machinery without an errorHandling block
	if strings.Contains(contentStr, "IsMaxRetriesExceeded") {
		t.Error("IsMaxRetriesExceeded should not be generated without errorHandling")
	}
	if strings.Contains(contentStr, "math.Pow") {
		t.Error("Exponential backoff should not be generated without errorHandling")
	}
	if strings.Contains(contentStr, "jitterRand") {
		t.Error("Jitter vars should not be generated without errorHandling")
	}

	// Failed has no requeue configured, so RetryBackoff falls back to 30s
	if !strings.Contains(contentStr, "return 30 * time.Second") {
		t.Error("Expected fixed 30s RetryBackoff for error state without requeue")
	}
}

func TestGenerator_NoErrorHandlingRequeueBackoff(t *testing.T) {
	// An error state WITH a requeue interval retries on that interval.
	sm := &schema.StateMachine{
		APIVersion: "sextant.jomcgi.dev/v1alpha1",
		Kind:       "StateMachine",
		Metadata: schema.Metadata{
			Name:    "TestResource",
			Group:   "test.io",
			Version: "v1alpha1",
		},
		Status: schema.StatusConfig{PhaseField: "phase"},
		States: []schema.State{
			{Name: "Pending", Initial: true},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true, Requeue: schema.Duration{Duration: 5 * time.Minute}},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	gen, err := codegen.New(codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	})
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	content, err := os.ReadFile(filepath.Join(tmpDir, "test_resource_transitions.go"))
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	if !strings.Contains(string(content), "return s.RequeueAfter()") {
		t.Error("Expected requeue-based RetryBackoff for error state with requeue interval")
	}
}

func TestGenerator_CustomErrorHandling(t *testing.T) {
	// Create a state machine with custom errorHandling
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
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
		ErrorHandling: &schema.ErrorHandling{
			MaxRetries: 5,
			Backoff: schema.BackoffConfig{
				Base:       schema.Duration{Duration: 2 * time.Second},
				Multiplier: 1.5,
				Max:        schema.Duration{Duration: 10 * time.Minute},
				Jitter:     0.2,
			},
		},
	}

	// Create temp output directory
	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated transitions file
	transitionsPath := filepath.Join(tmpDir, "test_resource_transitions.go")
	content, err := os.ReadFile(transitionsPath)
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	contentStr := string(content)

	// Verify custom values are used
	if !strings.Contains(contentStr, "max := 5") {
		t.Error("Expected custom maxRetries=5 in generated code")
	}

	// Custom base is 2s = 2_000_000_000 nanoseconds
	if !strings.Contains(contentStr, "time.Duration(2000000000)") {
		t.Error("Expected custom base=2s in generated code")
	}

	// Custom multiplier is 1.5
	if !strings.Contains(contentStr, "multiplier := 1.5") {
		t.Error("Expected custom multiplier=1.5 in generated code")
	}

	// Custom max is 10m = 600_000_000_000 nanoseconds
	if !strings.Contains(contentStr, "time.Duration(600000000000)") {
		t.Error("Expected custom max=10m in generated code")
	}

	// Custom jitter is 0.2
	if !strings.Contains(contentStr, "jitter := 0.2") {
		t.Error("Expected custom jitter=0.2 in generated code")
	}
}

func TestGenerator_SpecChangeHandling_Disabled(t *testing.T) {
	// By default, spec change handling is disabled
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

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated status file
	statusPath := filepath.Join(tmpDir, "test_resource_status.go")
	content, err := os.ReadFile(statusPath)
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	contentStr := string(content)

	// When spec change handling is disabled, HasSpecChanged should NOT be generated
	if strings.Contains(contentStr, "HasSpecChanged") {
		t.Error("HasSpecChanged should NOT be generated when spec change handling is disabled")
	}
	if strings.Contains(contentStr, "UpdateObservedGeneration") {
		t.Error("UpdateObservedGeneration should NOT be generated when spec change handling is disabled")
	}
}

func TestGenerator_SpecChangeHandling_Enabled(t *testing.T) {
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
		SpecChangeHandling: &schema.SpecChangeHandling{
			Enabled: true,
			// ObservedGenerationField not specified - should use default
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated status file
	statusPath := filepath.Join(tmpDir, "test_resource_status.go")
	content, err := os.ReadFile(statusPath)
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	contentStr := string(content)

	// Verify HasSpecChanged is generated with default field name
	if !strings.Contains(contentStr, "func HasSpecChanged") {
		t.Error("HasSpecChanged should be generated when spec change handling is enabled")
	}
	if !strings.Contains(contentStr, "r.Status.ObservedGeneration") {
		t.Error("Expected default field name 'ObservedGeneration' in HasSpecChanged")
	}

	// Verify UpdateObservedGeneration is generated
	if !strings.Contains(contentStr, "func UpdateObservedGeneration") {
		t.Error("UpdateObservedGeneration should be generated when spec change handling is enabled")
	}
}

func TestGenerator_SpecChangeHandling_CustomField(t *testing.T) {
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
		SpecChangeHandling: &schema.SpecChangeHandling{
			Enabled:                 true,
			ObservedGenerationField: "lastObservedGen",
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated status file
	statusPath := filepath.Join(tmpDir, "test_resource_status.go")
	content, err := os.ReadFile(statusPath)
	if err != nil {
		t.Fatalf("Failed to read generated file: %v", err)
	}

	contentStr := string(content)

	// Verify custom field name is used
	if !strings.Contains(contentStr, "r.Status.LastObservedGen") {
		t.Error("Expected custom field name 'LastObservedGen' in generated code")
	}
}

func TestGenerator_Metrics_Disabled(t *testing.T) {
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
		// Metrics not enabled
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Metrics file should NOT be generated
	metricsPath := filepath.Join(tmpDir, "test_resource_metrics.go")
	if _, err := os.Stat(metricsPath); !os.IsNotExist(err) {
		t.Error("Metrics file should NOT be generated when metrics is disabled")
	}
}

func TestGenerator_Metrics_Enabled(t *testing.T) {
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
			{Name: "Creating"},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Creating",
				Action: "StartCreation",
			},
			{
				From:   schema.TransitionSource{States: []string{"Creating"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending", "Creating"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
		Observability: schema.Observability{
			Metrics: true,
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Metrics file should be generated
	metricsPath := filepath.Join(tmpDir, "test_resource_metrics.go")
	content, err := os.ReadFile(metricsPath)
	if err != nil {
		t.Fatalf("Failed to read metrics file: %v", err)
	}

	contentStr := string(content)

	// Verify all expected metrics are generated
	expectedMetrics := []string{
		"testresource_reconcile_total",
		"testresource_reconcile_duration_seconds",
		"testresource_resource_phase",
		"testresource_errors_total",
		"testresource_state_duration_seconds",
	}

	for _, metric := range expectedMetrics {
		if !strings.Contains(contentStr, metric) {
			t.Errorf("Expected metric %q not found in generated code", metric)
		}
	}

	// Verify MetricsObserver is generated
	if !strings.Contains(contentStr, "type MetricsObserver struct") {
		t.Error("MetricsObserver struct not found in generated code")
	}

	if !strings.Contains(contentStr, "func NewMetricsObserver()") {
		t.Error("NewMetricsObserver function not found in generated code")
	}

	if !strings.Contains(contentStr, "func (m *MetricsObserver) OnTransition") {
		t.Error("OnTransition method not found in generated code")
	}

	// Verify cleanup function includes all states
	if !strings.Contains(contentStr, "CleanupResourceMetrics") {
		t.Error("CleanupResourceMetrics function not found in generated code")
	}

	// Verify states are referenced in cleanup
	expectedStates := []string{"Pending", "Creating", "Ready", "Failed"}
	for _, state := range expectedStates {
		if !strings.Contains(contentStr, `"`+state+`"`) {
			t.Errorf("State %q not found in CleanupResourceMetrics", state)
		}
	}
}

func TestGenerator_GuardWithCondition(t *testing.T) {
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
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Failed"}},
				To:     "Pending",
				Action: "Retry",
				Guard:  "retryable",
			},
		},
		Guards: map[string]schema.Guard{
			"retryable": {
				Description: "Can retry if under limit",
				MaxRetries:  3,
				Condition:   "s.RetryCount < 5",
			},
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated transitions file
	transitionsPath := filepath.Join(tmpDir, "test_resource_transitions.go")
	content, err := os.ReadFile(transitionsPath)
	if err != nil {
		t.Fatalf("Failed to read transitions file: %v", err)
	}

	contentStr := string(content)

	// Verify MaxRetries guard check is generated
	if !strings.Contains(contentStr, "s.RetryCount >= 3") {
		t.Error("MaxRetries guard check not found in generated code")
	}

	// Verify custom condition is generated
	if !strings.Contains(contentStr, "s.RetryCount < 5") {
		t.Error("Custom condition not found in generated code")
	}

	// Verify r variable is available for conditions
	if !strings.Contains(contentStr, "r := s.resource") {
		t.Error("Resource variable setup not found in generated code")
	}
}

func TestGenerator_MermaidDiagram_MultiSourceTransitions(t *testing.T) {
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
			{Name: "Creating"},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Creating",
				Action: "StartCreation",
			},
			{
				From:   schema.TransitionSource{States: []string{"Creating"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			// Multi-source transition
			{
				From:   schema.TransitionSource{States: []string{"Pending", "Creating"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
		Observability: schema.Observability{
			EmbedDiagram: true,
		},
	}

	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Read the generated types file which contains the Mermaid diagram
	typesPath := filepath.Join(tmpDir, "test_resource_types.go")
	content, err := os.ReadFile(typesPath)
	if err != nil {
		t.Fatalf("Failed to read types file: %v", err)
	}

	contentStr := string(content)

	// Verify the diagram includes BOTH source states for the multi-source transition
	// The bug was that only the first source state was rendered
	if !strings.Contains(contentStr, "Pending --> Failed: MarkFailed") {
		t.Error("Multi-source transition should render Pending --> Failed edge")
	}
	if !strings.Contains(contentStr, "Creating --> Failed: MarkFailed") {
		t.Error("Multi-source transition should render Creating --> Failed edge")
	}

	// Also verify single-source transitions still work
	if !strings.Contains(contentStr, "Pending --> Creating: StartCreation") {
		t.Error("Single-source transition Pending --> Creating should be rendered")
	}
	if !strings.Contains(contentStr, "Creating --> Ready: MarkReady") {
		t.Error("Single-source transition Creating --> Ready should be rendered")
	}
}

func TestGenerator_GeneratedCodeCompiles(t *testing.T) {
	// Create a comprehensive state machine
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
			{Name: "Creating"},
			{Name: "Ready", Terminal: true},
			{Name: "Failed", Error: true},
		},
		Transitions: []schema.Transition{
			{
				From:   schema.TransitionSource{States: []string{"Pending"}},
				To:     "Creating",
				Action: "StartCreation",
			},
			{
				From:   schema.TransitionSource{States: []string{"Creating"}},
				To:     "Ready",
				Action: "MarkReady",
			},
			{
				From:   schema.TransitionSource{States: []string{"Pending", "Creating"}},
				To:     "Failed",
				Action: "MarkFailed",
			},
		},
		ErrorHandling: &schema.ErrorHandling{
			MaxRetries: 3,
			Backoff: schema.BackoffConfig{
				Base:       schema.Duration{Duration: 1 * time.Second},
				Multiplier: 2,
				Max:        schema.Duration{Duration: 1 * time.Minute},
				Jitter:     0.1,
			},
		},
	}

	// Create temp output directory
	tmpDir, err := os.MkdirTemp("", "sextant-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	config := codegen.Config{
		OutputDir:     tmpDir,
		Package:       "testpkg",
		Module:        "github.com/test/operator",
		APIImportPath: "github.com/test/operator/api/v1alpha1",
	}

	gen, err := codegen.New(config)
	if err != nil {
		t.Fatalf("Failed to create generator: %v", err)
	}

	// This should not error - the Go formatter in the generator will catch syntax errors
	if err := gen.Generate(sm); err != nil {
		t.Fatalf("Generate failed: %v", err)
	}

	// Verify all expected files were generated
	expectedFiles := []string{
		"test_resource_phases.go",
		"test_resource_types.go",
		"test_resource_calculator.go",
		"test_resource_transitions.go",
		"test_resource_visit.go",
		"test_resource_observability.go",
		"test_resource_status.go",
	}

	for _, f := range expectedFiles {
		path := filepath.Join(tmpDir, f)
		if _, err := os.Stat(path); os.IsNotExist(err) {
			t.Errorf("Expected file %s was not generated", f)
		}
	}
}
