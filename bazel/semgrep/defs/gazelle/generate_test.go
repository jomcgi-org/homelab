package gazelle

import (
	"testing"

	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

func TestGenerateRules_PerFile(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "utils.py", "README.md"},
	}

	result := generateRules(args)

	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	// Sorted: main.py, utils.py
	assertRule(t, result.Gen[0], "main_semgrep_test", []string{"main.py"})
	assertRule(t, result.Gen[1], "utils_semgrep_test", []string{"utils.py"})

	if len(result.Imports) != len(result.Gen) {
		t.Errorf("imports count %d != gen count %d", len(result.Imports), len(result.Gen))
	}
}

func TestGenerateRules_SingleFile(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py"},
	}

	result := generateRules(args)

	if len(result.Gen) != 1 {
		t.Fatalf("expected 1 generated rule, got %d", len(result.Gen))
	}

	assertRule(t, result.Gen[0], "main_semgrep_test", []string{"main.py"})
}

func TestGenerateRules_InitPy(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"__init__.py", "main.py"},
	}

	result := generateRules(args)

	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	assertRule(t, result.Gen[0], "__init___semgrep_test", []string{"__init__.py"})
	assertRule(t, result.Gen[1], "main_semgrep_test", []string{"main.py"})
}

func TestGenerateRules_NoPythonFiles(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.go", "README.md", "BUILD"},
	}

	result := generateRules(args)

	if len(result.Gen) != 0 {
		t.Errorf("expected 0 rules for non-Python package, got %d", len(result.Gen))
	}
}

func TestGenerateRules_EmptyDirectory(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "empty",
		RegularFiles: []string{},
	}

	result := generateRules(args)

	if len(result.Gen) != 0 {
		t.Errorf("expected 0 rules for empty directory, got %d", len(result.Gen))
	}
}

func TestGenerateRules_Disabled(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     false,
				targetKinds: map[string]string{"py_venv_binary": ""},
				languages:   []string{"py"},
			},
		},
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py"},
	}

	result := generateRules(args)

	if len(result.Gen) != 0 {
		t.Errorf("expected 0 rules when disabled, got %d", len(result.Gen))
	}
}

func TestGenerateRules_WithExcludeRules(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:      true,
				excludeRules: []string{"no-requests", "no-eval-exec"},
				targetKinds:  map[string]string{"py_venv_binary": ""},
				languages:    []string{"py"},
			},
		},
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "utils.py"},
	}

	result := generateRules(args)

	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	for _, r := range result.Gen {
		if r.Attr("exclude_rules") == nil {
			t.Errorf("rule %q missing exclude_rules", r.Name())
		}
	}
}

func TestGenerateRules_TestFilesIncluded(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "main_test.py", "conftest.py"},
	}

	result := generateRules(args)

	if len(result.Gen) != 3 {
		t.Fatalf("expected 3 rules (including test files), got %d", len(result.Gen))
	}

	assertRule(t, result.Gen[0], "conftest_semgrep_test", []string{"conftest.py"})
	assertRule(t, result.Gen[1], "main_semgrep_test", []string{"main.py"})
	assertRule(t, result.Gen[2], "main_test_semgrep_test", []string{"main_test.py"})
}

func TestScannableFiles(t *testing.T) {
	tests := []struct {
		name  string
		files []string
		langs []string
		want  []string
	}{
		{
			name:  "mixed files python only",
			files: []string{"main.go", "utils.py", "main.py", "BUILD"},
			langs: []string{"py"},
			want:  []string{"main.py", "utils.py"},
		},
		{
			name:  "no python files",
			files: []string{"main.go", "README.md"},
			langs: []string{"py"},
			want:  nil,
		},
		{
			name:  "empty",
			files: []string{},
			langs: []string{"py"},
			want:  nil,
		},
		{
			name:  "py in filename but not extension",
			files: []string{"pytest.ini", "deploy.yaml"},
			langs: []string{"py"},
			want:  nil,
		},
		{
			name:  "sorted output",
			files: []string{"z.py", "a.py", "m.py"},
			langs: []string{"py"},
			want:  []string{"a.py", "m.py", "z.py"},
		},
		{
			name:  "multi-language",
			files: []string{"main.go", "utils.py", "BUILD"},
			langs: []string{"py", "go"},
			want:  []string{"main.go", "utils.py"},
		},
		{
			name:  "go only",
			files: []string{"main.go", "utils.py"},
			langs: []string{"go"},
			want:  []string{"main.go"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := scannableFiles(tc.files, tc.langs)
			if len(got) != len(tc.want) {
				t.Fatalf("scannableFiles() = %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("[%d] = %q, want %q", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestSortedExcludeRules(t *testing.T) {
	got := sortedExcludeRules([]string{"no-eval", "no-assert", "no-exec"})
	want := []string{"no-assert", "no-eval", "no-exec"}

	if len(got) != len(want) {
		t.Fatalf("got %d items, want %d", len(got), len(want))
	}
	for i, v := range got {
		if v != want[i] {
			t.Errorf("[%d] = %q, want %q", i, v, want[i])
		}
	}
}

func TestSortedExcludeRules_DoesNotMutateInput(t *testing.T) {
	input := []string{"c", "a", "b"}
	original := append([]string{}, input...)

	_ = sortedExcludeRules(input)

	for i, v := range input {
		if v != original[i] {
			t.Errorf("input was mutated: input[%d] = %q, original = %q", i, v, original[i])
		}
	}
}

// --- Helper functions for building test BUILD files ---

// buildFileWithRules creates a BUILD file with the given rules inserted.
func buildFileWithRules(rules ...*rule.Rule) *rule.File {
	f, _ := rule.LoadData("BUILD", "", nil)
	for _, r := range rules {
		r.Insert(f)
	}
	return f
}

// newPyBinary creates a py_venv_binary rule with the given name and main attribute.
func newPyBinary(name, main string) *rule.Rule {
	r := rule.NewRule("py_venv_binary", name)
	r.SetAttr("main", main)
	return r
}

// newPyLibrary creates a py_library rule with the given name and srcs.
func newPyLibrary(name string, srcs []string) *rule.Rule {
	r := rule.NewRule("py_library", name)
	r.SetAttr("srcs", srcs)
	return r
}

// newPyLibraryWithDeps creates a py_library rule with srcs and deps.
func newPyLibraryWithDeps(name string, srcs []string, deps []string) *rule.Rule {
	r := rule.NewRule("py_library", name)
	r.SetAttr("srcs", srcs)
	r.SetAttr("deps", deps)
	return r
}

// newPyBinaryWithDeps creates a py_venv_binary rule with main and deps.
func newPyBinaryWithDeps(name, main string, deps []string) *rule.Rule {
	r := rule.NewRule("py_venv_binary", name)
	r.SetAttr("main", main)
	r.SetAttr("deps", deps)
	return r
}

// --- Tests for binary-based semgrep test generation ---

func TestGenerateRules_WithBinary(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	binaryRule := newPyBinary("main", "main.py")
	buildFile := buildFileWithRules(binaryRule)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"__init__.py", "main.py", "utils.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 1 semgrep_target_test for binary + 2 per-file semgrep_test for orphans
	if len(result.Gen) != 3 {
		t.Fatalf("expected 3 generated rules, got %d", len(result.Gen))
	}

	// First rule: semgrep_target_test for the binary
	targetRule := result.Gen[0]
	if targetRule.Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want %q", targetRule.Kind(), "semgrep_target_test")
	}
	if targetRule.Name() != "main_semgrep_test" {
		t.Errorf("rule[0] name = %q, want %q", targetRule.Name(), "main_semgrep_test")
	}
	if targetRule.AttrString("target") != ":main" {
		t.Errorf("rule[0] target = %q, want %q", targetRule.AttrString("target"), ":main")
	}
	targetRules := targetRule.AttrStrings("rules")
	if len(targetRules) != 1 || targetRules[0] != "//bazel/semgrep/rules:python_rules" {
		t.Errorf("rule[0] rules = %v, want [//bazel/semgrep/rules:python_rules]", targetRules)
	}

	// Remaining rules: per-file semgrep_test for orphans (sorted: __init__.py, utils.py)
	assertRule(t, result.Gen[1], "__init___semgrep_test", []string{"__init__.py"})
	assertRule(t, result.Gen[2], "utils_semgrep_test", []string{"utils.py"})

	if len(result.Imports) != len(result.Gen) {
		t.Errorf("imports count %d != gen count %d", len(result.Imports), len(result.Gen))
	}
}

func TestGenerateRules_MultipleBinaries(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	scraperBin := newPyBinary("scraper", "scraper_main.py")
	embedderBin := newPyBinary("embedder", "embedder_main.py")
	buildFile := buildFileWithRules(scraperBin, embedderBin)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/pipeline",
		RegularFiles: []string{"__init__.py", "config.py", "embedder_main.py", "models.py", "scraper_main.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 2 semgrep_target_test + 3 per-file orphan semgrep_test
	if len(result.Gen) != 5 {
		t.Fatalf("expected 5 generated rules, got %d", len(result.Gen))
	}

	// Binary rules (sorted by binary name: embedder, scraper)
	if result.Gen[0].Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want semgrep_target_test", result.Gen[0].Kind())
	}
	if result.Gen[0].AttrString("target") != ":embedder" {
		t.Errorf("rule[0] target = %q, want :embedder", result.Gen[0].AttrString("target"))
	}

	if result.Gen[1].Kind() != "semgrep_target_test" {
		t.Errorf("rule[1] kind = %q, want semgrep_target_test", result.Gen[1].Kind())
	}
	if result.Gen[1].AttrString("target") != ":scraper" {
		t.Errorf("rule[1] target = %q, want :scraper", result.Gen[1].AttrString("target"))
	}

	// Orphan per-file rules (sorted: __init__.py, config.py, models.py)
	assertRule(t, result.Gen[2], "__init___semgrep_test", []string{"__init__.py"})
	assertRule(t, result.Gen[3], "config_semgrep_test", []string{"config.py"})
	assertRule(t, result.Gen[4], "models_semgrep_test", []string{"models.py"})
}

func TestGenerateRules_NoBinaries_FallsBackToPerFile(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	lib := newPyLibrary("mylib", []string{"lib.py", "helpers.py"})
	buildFile := buildFileWithRules(lib)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"helpers.py", "lib.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// No binaries, so all files get per-file semgrep_test
	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	assertRule(t, result.Gen[0], "helpers_semgrep_test", []string{"helpers.py"})
	assertRule(t, result.Gen[1], "lib_semgrep_test", []string{"lib.py"})
}

func TestGenerateRules_BinaryWithExcludeRules(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:      true,
				excludeRules: []string{"no-requests", "no-eval-exec"},
				targetKinds:  map[string]string{"py_venv_binary": ""},
				languages:    []string{"py"},
			},
		},
	}

	binaryRule := newPyBinary("server", "server.py")
	buildFile := buildFileWithRules(binaryRule)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py", "utils.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	// The semgrep_target_test for the binary should have exclude_rules
	targetRule := result.Gen[0]
	if targetRule.Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want semgrep_target_test", targetRule.Kind())
	}
	if targetRule.Attr("exclude_rules") == nil {
		t.Error("semgrep_target_test rule missing exclude_rules attribute")
	}

	// The orphan per-file rule should also have exclude_rules
	orphanRule := result.Gen[1]
	if orphanRule.Kind() != "semgrep_test" {
		t.Errorf("rule[1] kind = %q, want semgrep_test", orphanRule.Kind())
	}
	if orphanRule.Attr("exclude_rules") == nil {
		t.Error("orphan semgrep_test rule missing exclude_rules attribute")
	}
}

func TestGenerateRules_StaleTargetTestsRemoved(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	// BUILD file has an old semgrep_target_test that no longer corresponds to a binary
	oldTargetTest := rule.NewRule("semgrep_target_test", "old_binary_semgrep_test")
	oldTargetTest.SetAttr("target", ":old_binary")
	buildFile := buildFileWithRules(oldTargetTest)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Should generate per-file rules (no binaries in BUILD)
	if len(result.Gen) != 1 {
		t.Fatalf("expected 1 generated rule, got %d", len(result.Gen))
	}

	// The stale semgrep_target_test should appear in Empty
	foundStale := false
	for _, r := range result.Empty {
		if r.Kind() == "semgrep_target_test" && r.Name() == "old_binary_semgrep_test" {
			foundStale = true
			break
		}
	}
	if !foundStale {
		t.Error("stale semgrep_target_test 'old_binary_semgrep_test' should appear in result.Empty")
	}
}

func TestGenerateRules_PyBinaryKindAlsoDetected(t *testing.T) {
	// py_binary is not in default targetKinds, but can be configured
	c := configWithTargetKinds(map[string]string{
		"py_venv_binary": "",
		"py_binary":      "",
	})

	// Use py_binary instead of py_venv_binary
	pyBin := rule.NewRule("py_binary", "cli")
	pyBin.SetAttr("main", "cli.py")
	buildFile := buildFileWithRules(pyBin)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/cli",
		RegularFiles: []string{"cli.py", "helpers.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 1 semgrep_target_test for py_binary + 1 per-file semgrep_test for orphan
	if len(result.Gen) != 2 {
		t.Fatalf("expected 2 generated rules, got %d", len(result.Gen))
	}

	// First rule: semgrep_target_test for the py_binary
	targetRule := result.Gen[0]
	if targetRule.Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want semgrep_target_test", targetRule.Kind())
	}
	if targetRule.Name() != "cli_semgrep_test" {
		t.Errorf("rule[0] name = %q, want cli_semgrep_test", targetRule.Name())
	}
	if targetRule.AttrString("target") != ":cli" {
		t.Errorf("rule[0] target = %q, want :cli", targetRule.AttrString("target"))
	}

	// Second rule: per-file semgrep_test for orphan
	assertRule(t, result.Gen[1], "helpers_semgrep_test", []string{"helpers.py"})
}

func TestGenerateRules_BinaryWithLocalLibDeps(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	// Simulate a package like knowledge_graph/app:
	// - scraper binary depends on :config and :models (local libraries)
	// - config.py and models.py are srcs of those libraries
	// - __init__.py is NOT a dep of any binary
	configLib := newPyLibrary("config", []string{"config.py"})
	modelsLib := newPyLibrary("models", []string{"models.py"})
	scraperMain := newPyLibraryWithDeps("scraper_main", []string{"scraper_main.py"}, []string{":config", ":models"})
	scraperBin := newPyBinaryWithDeps("scraper", "scraper_main.py", []string{":scraper_main"})
	buildFile := buildFileWithRules(configLib, modelsLib, scraperMain, scraperBin)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/pipeline",
		RegularFiles: []string{"__init__.py", "config.py", "models.py", "scraper_main.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 1 semgrep_target_test for binary + 1 per-file semgrep_test for __init__.py only
	// config.py and models.py are covered by the binary's transitive local deps
	if len(result.Gen) != 2 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 2 generated rules, got %d: %v", len(result.Gen), names)
	}

	// First rule: semgrep_target_test for the binary
	if result.Gen[0].Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want semgrep_target_test", result.Gen[0].Kind())
	}
	if result.Gen[0].AttrString("target") != ":scraper" {
		t.Errorf("rule[0] target = %q, want :scraper", result.Gen[0].AttrString("target"))
	}

	// Second rule: only __init__.py as orphan
	assertRule(t, result.Gen[1], "__init___semgrep_test", []string{"__init__.py"})
}

func TestGenerateRules_MultipleBinariesWithSharedDeps(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	// Two binaries that share some deps. Like knowledge_graph/app.
	configLib := newPyLibrary("config", []string{"config.py"})
	modelsLib := newPyLibrary("models", []string{"models.py"})
	storageLib := newPyLibraryWithDeps("storage", []string{"storage.py"}, []string{":models"})
	telemetryLib := newPyLibrary("telemetry", []string{"telemetry.py"})
	chunkerLib := newPyLibraryWithDeps("chunker", []string{"chunker.py"}, []string{":models"})

	scraperMainLib := newPyLibraryWithDeps("scraper_main", []string{"scraper_main.py"},
		[]string{":config", ":models", ":storage", ":telemetry"})
	scraperBin := newPyBinaryWithDeps("scraper", "scraper_main.py", []string{":scraper_main"})

	embedderMainLib := newPyLibraryWithDeps("embedder_main", []string{"embedder_main.py"},
		[]string{":chunker", ":config", ":storage", ":telemetry"})
	embedderBin := newPyBinaryWithDeps("embedder", "embedder_main.py", []string{":embedder_main"})

	buildFile := buildFileWithRules(
		configLib, modelsLib, storageLib, telemetryLib, chunkerLib,
		scraperMainLib, scraperBin, embedderMainLib, embedderBin,
	)

	args := language.GenerateArgs{
		Config: c,
		Dir:    "/tmp/test",
		Rel:    "services/pipeline",
		RegularFiles: []string{
			"__init__.py", "chunker.py", "config.py", "embedder_main.py",
			"models.py", "scraper_main.py", "storage.py", "telemetry.py",
		},
		File: buildFile,
	}

	result := generateRules(args)

	// Expect: 2 semgrep_target_test (embedder, scraper) + 1 orphan (__init__.py)
	// All other files are transitively reachable from the binaries
	if len(result.Gen) != 3 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 3 generated rules, got %d: %v", len(result.Gen), names)
	}

	if result.Gen[0].Kind() != "semgrep_target_test" {
		t.Errorf("rule[0] kind = %q, want semgrep_target_test", result.Gen[0].Kind())
	}
	if result.Gen[0].AttrString("target") != ":embedder" {
		t.Errorf("rule[0] target = %q, want :embedder", result.Gen[0].AttrString("target"))
	}

	if result.Gen[1].Kind() != "semgrep_target_test" {
		t.Errorf("rule[1] kind = %q, want semgrep_target_test", result.Gen[1].Kind())
	}
	if result.Gen[1].AttrString("target") != ":scraper" {
		t.Errorf("rule[1] target = %q, want :scraper", result.Gen[1].AttrString("target"))
	}

	// Only __init__.py is orphaned
	assertRule(t, result.Gen[2], "__init___semgrep_test", []string{"__init__.py"})
}

func TestGenerateRules_StalePerFileTestsRemovedWhenBinaryCovers(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	// Simulate: existing BUILD has per-file semgrep_tests AND a binary with deps
	configLib := newPyLibrary("config", []string{"config.py"})
	modelsLib := newPyLibrary("models", []string{"models.py"})
	scraperMainLib := newPyLibraryWithDeps("scraper_main", []string{"scraper_main.py"}, []string{":config", ":models"})
	scraperBin := newPyBinaryWithDeps("scraper", "scraper_main.py", []string{":scraper_main"})

	// Old per-file semgrep tests
	oldInitTest := rule.NewRule("semgrep_test", "__init___semgrep_test")
	oldInitTest.SetAttr("srcs", []string{"__init__.py"})
	oldConfigTest := rule.NewRule("semgrep_test", "config_semgrep_test")
	oldConfigTest.SetAttr("srcs", []string{"config.py"})
	oldModelsTest := rule.NewRule("semgrep_test", "models_semgrep_test")
	oldModelsTest.SetAttr("srcs", []string{"models.py"})
	oldScraperTest := rule.NewRule("semgrep_test", "scraper_main_semgrep_test")
	oldScraperTest.SetAttr("srcs", []string{"scraper_main.py"})

	buildFile := buildFileWithRules(
		configLib, modelsLib, scraperMainLib, scraperBin,
		oldInitTest, oldConfigTest, oldModelsTest, oldScraperTest,
	)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/pipeline",
		RegularFiles: []string{"__init__.py", "config.py", "models.py", "scraper_main.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 1 semgrep_target_test + 1 orphan (init)
	if len(result.Gen) != 2 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 2 generated rules, got %d: %v", len(result.Gen), names)
	}

	// Verify stale rules
	// config_semgrep_test, models_semgrep_test, scraper_main_semgrep_test should be stale
	staleNames := make(map[string]bool)
	for _, r := range result.Empty {
		staleNames[r.Kind()+"/"+r.Name()] = true
		t.Logf("stale: %s/%s", r.Kind(), r.Name())
	}

	expectedStale := []string{
		"semgrep_test/config_semgrep_test",
		"semgrep_test/models_semgrep_test",
		"semgrep_test/scraper_main_semgrep_test",
	}
	for _, s := range expectedStale {
		if !staleNames[s] {
			t.Errorf("expected %q in Empty set but not found", s)
		}
	}

	// __init___semgrep_test should NOT be stale (it's an orphan)
	if staleNames["semgrep_test/__init___semgrep_test"] {
		t.Error("__init___semgrep_test should NOT be in Empty set")
	}
}

// newGoBinary creates a go_binary rule with the given name.
func newGoBinary(name string) *rule.Rule {
	r := rule.NewRule("go_binary", name)
	return r
}

// TestGenerateRules_GoBinaryTargetKind verifies that go_binary entries in the
// BUILD file generate a semgrep_target_test when go_binary is in targetKinds
// (it is present in the defaultTargetKinds map).
func TestGenerateRules_GoBinaryTargetKind(t *testing.T) {
	// Use default config — go_binary is in defaultTargetKinds with attr "".
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	goBin := newGoBinary("server")
	buildFile := buildFileWithRules(goBin)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "cmd/server",
		RegularFiles: []string{"main.go", "handler.go"},
		File:         buildFile,
	}

	result := generateRules(args)

	// go_binary is in defaultTargetKinds with the default languages (["py"]).
	// Since no .py files exist, scan files is empty but the go_binary target
	// is still in targetKinds so we get a semgrep_target_test for it.
	targetTests := 0
	for _, r := range result.Gen {
		if r.Kind() == "semgrep_target_test" {
			targetTests++
			if r.Name() != "server_semgrep_test" {
				t.Errorf("semgrep_target_test name = %q, want server_semgrep_test", r.Name())
			}
			if r.AttrString("target") != ":server" {
				t.Errorf("semgrep_target_test target = %q, want :server", r.AttrString("target"))
			}
		}
	}
	if targetTests != 1 {
		t.Fatalf("expected 1 semgrep_target_test for go_binary, got %d (rules: %v)", targetTests, rulesKindNames(result.Gen))
	}
}

// TestGenerateRules_GoBinaryWithGoLanguage verifies go_binary with go language
// config generates semgrep_target_test and covers .go files.
func TestGenerateRules_GoBinaryWithGoLanguage(t *testing.T) {
	c := configWithLanguages(
		map[string]string{"go_binary": ""},
		[]string{"go"},
	)

	goBin := newGoBinary("server")
	buildFile := buildFileWithRules(goBin)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "cmd/server",
		RegularFiles: []string{"main.go", "handler.go"},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect 1 semgrep_target_test for go_binary + 2 orphan per-file tests
	// (go_binary has no srcs attr in the rule, so no files are "covered").
	targetTests := 0
	for _, r := range result.Gen {
		if r.Kind() == "semgrep_target_test" {
			targetTests++
			if r.AttrString("target") != ":server" {
				t.Errorf("target = %q, want :server", r.AttrString("target"))
			}
			// rules attr should include golang_rules
			rules := r.AttrStrings("rules")
			foundGo := false
			for _, rl := range rules {
				if rl == "//bazel/semgrep/rules:golang_rules" {
					foundGo = true
				}
			}
			if !foundGo {
				t.Errorf("go_binary semgrep_target_test rules %v missing golang_rules", rules)
			}
		}
	}
	if targetTests != 1 {
		t.Fatalf("expected 1 semgrep_target_test for go_binary, got %d", targetTests)
	}
}

// TestWalkLocalDeps_CircularDependencyGuard verifies that the visited map in
// walkLocalDeps prevents infinite recursion when lib_a depends on lib_b and
// lib_b depends on lib_a.
func TestWalkLocalDeps_CircularDependencyGuard(t *testing.T) {
	// Build a circular dep graph: lib_a → lib_b → lib_a
	libA := rule.NewRule("py_library", "lib_a")
	libA.SetAttr("srcs", []string{"a.py"})
	libA.SetAttr("deps", []string{":lib_b"})

	libB := rule.NewRule("py_library", "lib_b")
	libB.SetAttr("srcs", []string{"b.py"})
	libB.SetAttr("deps", []string{":lib_a"})

	binary := newPyBinaryWithDeps("app", "main.py", []string{":lib_a"})

	buildFile := buildFileWithRules(libA, libB, binary)

	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "a.py", "b.py"},
		File:         buildFile,
	}

	// This must terminate — if the circular guard is broken, it would loop forever.
	// We rely on the test timeout to catch an infinite loop.
	result := generateRules(args)

	// We expect 1 semgrep_target_test for the binary.
	targetTests := 0
	for _, r := range result.Gen {
		if r.Kind() == "semgrep_target_test" {
			targetTests++
		}
	}
	if targetTests != 1 {
		t.Errorf("expected 1 semgrep_target_test, got %d", targetTests)
	}

	// a.py and b.py should be covered (transitively reachable from binary via lib_a → lib_b).
	for _, r := range result.Gen {
		if r.Kind() == "semgrep_test" {
			srcs := r.AttrStrings("srcs")
			for _, src := range srcs {
				if src == "a.py" || src == "b.py" {
					t.Errorf("file %q should be covered by the binary's transitive deps, not orphaned", src)
				}
			}
		}
	}
}

// TestWalkLocalDeps_CircularDependencyBothDirections verifies the guard works
// even when the cycle starts from the second library (lib_b → lib_a → lib_b).
func TestWalkLocalDeps_CircularDependencyBothDirections(t *testing.T) {
	libA := rule.NewRule("py_library", "lib_a")
	libA.SetAttr("srcs", []string{"a.py"})
	libA.SetAttr("deps", []string{":lib_b"})

	libB := rule.NewRule("py_library", "lib_b")
	libB.SetAttr("srcs", []string{"b.py"})
	libB.SetAttr("deps", []string{":lib_a"})

	// Binary depends directly on lib_b this time, which then reaches lib_a.
	binary := newPyBinaryWithDeps("app", "main.py", []string{":lib_b"})

	buildFile := buildFileWithRules(libA, libB, binary)

	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "a.py", "b.py"},
		File:         buildFile,
	}

	// Must terminate without infinite loop.
	result := generateRules(args)

	targetTests := 0
	for _, r := range result.Gen {
		if r.Kind() == "semgrep_target_test" {
			targetTests++
		}
	}
	if targetTests != 1 {
		t.Errorf("expected 1 semgrep_target_test, got %d", targetTests)
	}
}

// TestRulesForLanguages_UnknownLanguage verifies that an unknown language key
// silently returns an empty slice rather than panicking or returning an error.
func TestRulesForLanguages_UnknownLanguage(t *testing.T) {
	got := rulesForLanguages([]string{"rust"})
	if len(got) != 0 {
		t.Errorf("rulesForLanguages([rust]) = %v, want empty", got)
	}
}

// TestRulesForLanguages_MixedKnownAndUnknown verifies that unknown languages
// are silently skipped while known languages still return their rules.
func TestRulesForLanguages_MixedKnownAndUnknown(t *testing.T) {
	got := rulesForLanguages([]string{"rust", "py", "haskell"})
	if len(got) != 1 {
		t.Fatalf("rulesForLanguages([rust py haskell]) = %v, want 1 entry", got)
	}
	if got[0] != "//bazel/semgrep/rules:python_rules" {
		t.Errorf("got[0] = %q, want //bazel/semgrep/rules:python_rules", got[0])
	}
}

// TestScannableFiles_UnknownLanguage verifies that an unknown language key
// produces no scannable files (no extension mapping exists).
func TestScannableFiles_UnknownLanguage(t *testing.T) {
	got := scannableFiles([]string{"main.rs", "lib.rs", "main.py"}, []string{"rust"})
	if len(got) != 0 {
		t.Errorf("scannableFiles with unknown lang rust = %v, want empty", got)
	}
}

// TestGenerateRules_UnknownLanguageNoOutput verifies that configuring an unknown
// language key produces no generated rules (no files match, no rules emitted).
func TestGenerateRules_UnknownLanguageNoOutput(t *testing.T) {
	c := configWithLanguages(
		map[string]string{"py_venv_binary": ""},
		[]string{"rust"},
	)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.rs", "lib.rs"},
	}

	result := generateRules(args)
	if len(result.Gen) != 0 {
		t.Errorf("expected 0 rules for unknown language, got %d: %v", len(result.Gen), rulesKindNames(result.Gen))
	}
}

// rulesKindNames is a helper for test failure messages.
func rulesKindNames(rules []*rule.Rule) []string {
	var out []string
	for _, r := range rules {
		out = append(out, r.Kind()+"/"+r.Name())
	}
	return out
}

// assertRule checks a rule's name, kind, and srcs.
func assertRule(t *testing.T, r *rule.Rule, wantName string, wantSrcs []string) {
	t.Helper()
	if r.Kind() != "semgrep_test" {
		t.Errorf("rule kind = %q, want %q", r.Kind(), "semgrep_test")
	}
	if r.Name() != wantName {
		t.Errorf("rule name = %q, want %q", r.Name(), wantName)
	}
	srcs := r.AttrStrings("srcs")
	if len(srcs) != len(wantSrcs) {
		t.Errorf("rule %q srcs = %v, want %v", r.Name(), srcs, wantSrcs)
		return
	}
	for i := range srcs {
		if srcs[i] != wantSrcs[i] {
			t.Errorf("rule %q srcs[%d] = %q, want %q", r.Name(), i, srcs[i], wantSrcs[i])
		}
	}
}

// --- Helper functions for config-driven generation tests ---

// newPy3Image creates a py3_image rule with a binary attr.
func newPy3Image(name, binary string) *rule.Rule {
	r := rule.NewRule("py3_image", name)
	r.SetAttr("binary", binary)
	return r
}

// configWithTargetKinds creates a config.Config with the given targetKinds
// and default languages (["py"]).
func configWithTargetKinds(kinds map[string]string) *config.Config {
	return &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				targetKinds: kinds,
				languages:   []string{"py"},
			},
		},
	}
}

// configWithLanguages creates a config.Config with the given targetKinds
// and languages.
func configWithLanguages(kinds map[string]string, langs []string) *config.Config {
	return &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				targetKinds: kinds,
				languages:   langs,
			},
		},
	}
}

// --- Tests for config-driven generation ---

func TestGenerateRules_Py3ImageTargetKind(t *testing.T) {
	// py3_image with cross-package binary attr → semgrep_target_test targets the binary
	c := configWithTargetKinds(map[string]string{
		"py_venv_binary": "",
		"py3_image":      "binary",
	})

	image := newPy3Image("scraper-image", "//services/knowledge_graph/app:scraper")
	buildFile := buildFileWithRules(image)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/knowledge_graph/deploy",
		RegularFiles: []string{},
		File:         buildFile,
	}

	result := generateRules(args)

	// Expect: 1 semgrep_target_test targeting the binary label from the image
	if len(result.Gen) != 1 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 1 generated rule, got %d: %v", len(result.Gen), names)
	}

	r := result.Gen[0]
	if r.Kind() != "semgrep_target_test" {
		t.Errorf("rule kind = %q, want semgrep_target_test", r.Kind())
	}
	if r.Name() != "scraper-image_semgrep_test" {
		t.Errorf("rule name = %q, want scraper-image_semgrep_test", r.Name())
	}
	if r.AttrString("target") != "//services/knowledge_graph/app:scraper" {
		t.Errorf("target = %q, want //services/knowledge_graph/app:scraper", r.AttrString("target"))
	}
	rulesList := r.AttrStrings("rules")
	if len(rulesList) != 1 || rulesList[0] != "//bazel/semgrep/rules:python_rules" {
		t.Errorf("rules = %v, want [//bazel/semgrep/rules:python_rules]", rulesList)
	}
}

func TestGenerateRules_Py3ImageLocalBinary(t *testing.T) {
	for _, tc := range []struct {
		name        string
		binaryLabel string
		imageName   string
	}{
		{
			name:        "relative label",
			binaryLabel: ":update",
			imageName:   "update_image",
		},
		{
			name:        "absolute label with image sorted first",
			binaryLabel: "//services/myapp:update",
			imageName:   "image",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// When both py_venv_binary and py3_image point to the same target,
			// deduplicate by canonical label, regardless of label spelling.
			c := configWithTargetKinds(map[string]string{
				"py_venv_binary": "",
				"py3_image":      "binary",
			})

			binary := newPyBinary("update", "update.py")
			image := newPy3Image(tc.imageName, tc.binaryLabel)
			buildFile := buildFileWithRules(binary, image)

			args := language.GenerateArgs{
				Config:       c,
				Dir:          "/tmp/test",
				Rel:          "services/myapp",
				RegularFiles: []string{"update.py"},
				File:         buildFile,
			}

			result := generateRules(args)

			// Expect: 1 semgrep_target_test (deduplicated) targeting the binary.
			targetTests := 0
			for _, r := range result.Gen {
				if r.Kind() == "semgrep_target_test" {
					targetTests++
					if r.Name() != "update_semgrep_test" {
						t.Errorf("name = %q, want update_semgrep_test", r.Name())
					}
					if r.AttrString("target") != ":update" {
						t.Errorf("target = %q, want :update", r.AttrString("target"))
					}
				}
			}
			if targetTests != 1 {
				var names []string
				for _, r := range result.Gen {
					names = append(names, r.Kind()+"/"+r.Name()+"→"+r.AttrString("target"))
				}
				t.Fatalf("expected 1 semgrep_target_test (deduplicated), got %d: %v", targetTests, names)
			}
		})
	}
}

func TestGenerateRules_LanguageRulesConfig(t *testing.T) {
	// Multi-language config → target test gets all rule configs
	c := configWithLanguages(
		map[string]string{"py_venv_binary": ""},
		[]string{"py", "go"},
	)

	binary := newPyBinary("server", "server.py")
	buildFile := buildFileWithRules(binary)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	if len(result.Gen) < 1 {
		t.Fatalf("expected at least 1 generated rule, got %d", len(result.Gen))
	}

	targetRule := result.Gen[0]
	if targetRule.Kind() != "semgrep_target_test" {
		t.Fatalf("rule[0] kind = %q, want semgrep_target_test", targetRule.Kind())
	}

	rules := targetRule.AttrStrings("rules")
	// Should contain both golang_rules and python_rules, sorted
	if len(rules) != 2 {
		t.Fatalf("rules = %v, want 2 entries", rules)
	}
	if rules[0] != "//bazel/semgrep/rules:golang_rules" {
		t.Errorf("rules[0] = %q, want //bazel/semgrep/rules:golang_rules", rules[0])
	}
	if rules[1] != "//bazel/semgrep/rules:python_rules" {
		t.Errorf("rules[1] = %q, want //bazel/semgrep/rules:python_rules", rules[1])
	}
}

func TestGenerateRules_MultiLangOrphans(t *testing.T) {
	// .py and .go files both get per-file tests when no binaries exist
	c := configWithLanguages(
		map[string]string{"py_venv_binary": ""},
		[]string{"py", "go"},
	)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"main.py", "utils.go"},
	}

	result := generateRules(args)

	// Expect: 2 per-file semgrep_test rules
	if len(result.Gen) != 2 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 2 generated rules, got %d: %v", len(result.Gen), names)
	}

	// .py file gets python_rules
	pyRule := result.Gen[0]
	if pyRule.Name() != "main_semgrep_test" {
		t.Errorf("rule[0] name = %q, want main_semgrep_test", pyRule.Name())
	}
	pyRules := pyRule.AttrStrings("rules")
	if len(pyRules) != 1 || pyRules[0] != "//bazel/semgrep/rules:python_rules" {
		t.Errorf("py file rules = %v, want [//bazel/semgrep/rules:python_rules]", pyRules)
	}

	// .go file gets golang_rules
	goRule := result.Gen[1]
	if goRule.Name() != "utils_semgrep_test" {
		t.Errorf("rule[1] name = %q, want utils_semgrep_test", goRule.Name())
	}
	goRules := goRule.AttrStrings("rules")
	if len(goRules) != 1 || goRules[0] != "//bazel/semgrep/rules:golang_rules" {
		t.Errorf("go file rules = %v, want [//bazel/semgrep/rules:golang_rules]", goRules)
	}
}

// --- Tests for SCA lockfile detection in generation ---

func TestGenerateRules_WithBinaryAndPipDeps(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				scaEnabled:  true,
				scaRules:    map[string]string{"pip": "//bazel/semgrep/rules:sca_python_rules"},
				lockfiles:   map[string]string{"pip": "//bazel/requirements:all.txt"},
				targetKinds: map[string]string{"py_venv_binary": ""},
				languages:   []string{"py"},
			},
		},
	}

	binary := newPyBinaryWithDeps("server", "server.py", []string{"@pip//requests", ":utils"})
	buildFile := buildFileWithRules(binary)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	if len(result.Gen) < 1 {
		t.Fatalf("expected at least 1 rule, got %d", len(result.Gen))
	}

	targetRule := result.Gen[0]
	if targetRule.Kind() != "semgrep_target_test" {
		t.Fatalf("rule[0] kind = %q, want semgrep_target_test", targetRule.Kind())
	}

	lockfiles := targetRule.AttrStrings("lockfiles")
	if len(lockfiles) != 1 || lockfiles[0] != "//bazel/requirements:all.txt" {
		t.Errorf("lockfiles = %v, want [//requirements:all.txt]", lockfiles)
	}

	scaRules := targetRule.AttrStrings("sca_rules")
	if len(scaRules) != 1 || scaRules[0] != "//bazel/semgrep/rules:sca_python_rules" {
		t.Errorf("sca_rules = %v, want [//bazel/semgrep/rules:sca_python_rules]", scaRules)
	}
}

func TestGenerateRules_SCADisabled(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				scaEnabled:  false,
				lockfiles:   map[string]string{"pip": "//bazel/requirements:all.txt"},
				targetKinds: map[string]string{"py_venv_binary": ""},
				languages:   []string{"py"},
			},
		},
	}

	binary := newPyBinaryWithDeps("server", "server.py", []string{"@pip//requests"})
	buildFile := buildFileWithRules(binary)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py"},
		File:         buildFile,
	}

	result := generateRules(args)
	targetRule := result.Gen[0]

	lockfiles := targetRule.AttrStrings("lockfiles")
	if len(lockfiles) != 0 {
		t.Errorf("lockfiles should be empty when SCA disabled, got %v", lockfiles)
	}
}

func TestGenerateRules_MultipleDepsMultipleEcosystems(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				scaEnabled:  true,
				scaRules:    copyScaRules(defaultScaRules),
				lockfiles:   map[string]string{"pip": "//bazel/requirements:all.txt", "gomod": "//:go.sum"},
				targetKinds: map[string]string{"py_venv_binary": ""},
				languages:   []string{"py"},
			},
		},
	}

	binary := newPyBinaryWithDeps("server", "server.py", []string{"@pip//requests", "@go_deps//example.com/pkg"})
	buildFile := buildFileWithRules(binary)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py"},
		File:         buildFile,
	}

	result := generateRules(args)
	targetRule := result.Gen[0]

	lockfiles := targetRule.AttrStrings("lockfiles")
	if len(lockfiles) != 2 {
		t.Fatalf("expected 2 lockfiles, got %v", lockfiles)
	}
	// Sorted: //:go.sum comes before //requirements:all.txt
	if lockfiles[0] != "//:go.sum" {
		t.Errorf("lockfiles[0] = %q, want //:go.sum", lockfiles[0])
	}
	if lockfiles[1] != "//bazel/requirements:all.txt" {
		t.Errorf("lockfiles[1] = %q, want //requirements:all.txt", lockfiles[1])
	}

	scaRules := targetRule.AttrStrings("sca_rules")
	if len(scaRules) != 2 {
		t.Fatalf("expected 2 sca_rules, got %v", scaRules)
	}
	// Sorted: //bazel/semgrep/rules:sca_golang_rules before //bazel/semgrep/rules:sca_python_rules
	if scaRules[0] != "//bazel/semgrep/rules:sca_golang_rules" {
		t.Errorf("sca_rules[0] = %q, want //bazel/semgrep/rules:sca_golang_rules", scaRules[0])
	}
	if scaRules[1] != "//bazel/semgrep/rules:sca_python_rules" {
		t.Errorf("sca_rules[1] = %q, want //bazel/semgrep/rules:sca_python_rules", scaRules[1])
	}
}

func TestGenerateRules_NoDepsNoLockfiles(t *testing.T) {
	c := &config.Config{
		Exts: map[string]interface{}{
			semgrepConfigKey: &semgrepConfig{
				enabled:     true,
				scaEnabled:  true,
				scaRules:    copyScaRules(defaultScaRules),
				lockfiles:   map[string]string{"pip": "//bazel/requirements:all.txt"},
				targetKinds: map[string]string{"py_venv_binary": ""},
				languages:   []string{"py"},
			},
		},
	}

	binary := newPyBinary("server", "server.py")
	buildFile := buildFileWithRules(binary)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/myapp",
		RegularFiles: []string{"server.py"},
		File:         buildFile,
	}

	result := generateRules(args)
	targetRule := result.Gen[0]

	lockfiles := targetRule.AttrStrings("lockfiles")
	if len(lockfiles) != 0 {
		t.Errorf("lockfiles should be empty when target has no external deps, got %v", lockfiles)
	}
}

func TestDetectLockfiles(t *testing.T) {
	tests := []struct {
		name       string
		deps       []string
		scaEnabled bool
		lockfiles  map[string]string
		want       []string
	}{
		{
			name:       "pip deps detected",
			deps:       []string{"@pip//requests", "@pip//flask"},
			scaEnabled: true,
			lockfiles:  map[string]string{"pip": "//bazel/requirements:all.txt"},
			want:       []string{"//bazel/requirements:all.txt"},
		},
		{
			name:       "go deps detected",
			deps:       []string{"@go_deps//example.com/pkg"},
			scaEnabled: true,
			lockfiles:  map[string]string{"gomod": "//:go.sum"},
			want:       []string{"//:go.sum"},
		},
		{
			name:       "pnpm deps detected",
			deps:       []string{"@npm//react"},
			scaEnabled: true,
			lockfiles:  map[string]string{"pnpm": "//:pnpm-lock.yaml"},
			want:       []string{"//:pnpm-lock.yaml"},
		},
		{
			name:       "no external deps",
			deps:       []string{":local_lib", "//other:target"},
			scaEnabled: true,
			lockfiles:  map[string]string{"pip": "//bazel/requirements:all.txt"},
			want:       nil,
		},
		{
			name:       "sca disabled",
			deps:       []string{"@pip//requests"},
			scaEnabled: false,
			lockfiles:  map[string]string{"pip": "//bazel/requirements:all.txt"},
			want:       nil,
		},
		{
			name:       "multiple ecosystems sorted",
			deps:       []string{"@pip//requests", "@go_deps//example.com/pkg"},
			scaEnabled: true,
			lockfiles:  map[string]string{"pip": "//bazel/requirements:all.txt", "gomod": "//:go.sum"},
			want:       []string{"//:go.sum", "//bazel/requirements:all.txt"},
		},
		{
			name:       "ecosystem without lockfile config",
			deps:       []string{"@pip//requests"},
			scaEnabled: true,
			lockfiles:  map[string]string{"gomod": "//:go.sum"},
			want:       nil,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := rule.NewRule("py_venv_binary", "test")
			r.SetAttr("deps", tc.deps)

			cfg := &semgrepConfig{
				scaEnabled: tc.scaEnabled,
				lockfiles:  tc.lockfiles,
			}

			got := detectLockfiles(r, cfg)
			if len(got) != len(tc.want) {
				t.Fatalf("detectLockfiles() = %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("[%d] = %q, want %q", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestGenerateRules_TargetKindNotInConfig(t *testing.T) {
	// py3_image NOT in config → ignored, falls back to per-file
	c := configWithTargetKinds(map[string]string{
		"py_venv_binary": "",
	})

	image := newPy3Image("scraper-image", "//services/app:scraper")
	buildFile := buildFileWithRules(image)

	args := language.GenerateArgs{
		Config:       c,
		Dir:          "/tmp/test",
		Rel:          "services/deploy",
		RegularFiles: []string{"deploy.py"},
		File:         buildFile,
	}

	result := generateRules(args)

	// py3_image should be ignored since it's not in targetKinds.
	// Only the .py file should get a per-file semgrep_test.
	if len(result.Gen) != 1 {
		var names []string
		for _, r := range result.Gen {
			names = append(names, r.Kind()+"/"+r.Name())
		}
		t.Fatalf("expected 1 generated rule (per-file), got %d: %v", len(result.Gen), names)
	}

	r := result.Gen[0]
	if r.Kind() != "semgrep_test" {
		t.Errorf("rule kind = %q, want semgrep_test", r.Kind())
	}
	assertRule(t, r, "deploy_semgrep_test", []string{"deploy.py"})
}
