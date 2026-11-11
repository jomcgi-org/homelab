package gazelle

import (
	"testing"

	"github.com/bazelbuild/bazel-gazelle/label"
)

func TestNewLanguage(t *testing.T) {
	lang := NewLanguage()
	if lang == nil {
		t.Fatal("NewLanguage() returned nil")
	}

	_, ok := lang.(*argoCDLang)
	if !ok {
		t.Error("NewLanguage() should return *argoCDLang")
	}
}

func TestArgoCDLang_Name(t *testing.T) {
	lang := NewLanguage()
	name := lang.Name()

	if name != languageName {
		t.Errorf("Name() = %q, want %q", name, languageName)
	}
	if name != "argocd" {
		t.Errorf("Name() = %q, want %q", name, "argocd")
	}
}

func TestArgoCDLang_KnownDirectives(t *testing.T) {
	lang := NewLanguage()
	directives := lang.KnownDirectives()

	expected := []string{
		"argocd",
		"argocd_enabled",
		"argocd_generate_manifests",
	}

	if len(directives) != len(expected) {
		t.Errorf("KnownDirectives() returned %d directives, want %d", len(directives), len(expected))
	}

	directiveSet := make(map[string]bool)
	for _, d := range directives {
		directiveSet[d] = true
	}

	for _, e := range expected {
		if !directiveSet[e] {
			t.Errorf("KnownDirectives() missing directive %q", e)
		}
	}
}

func TestArgoCDLang_Kinds(t *testing.T) {
	lang := NewLanguage()
	kinds := lang.Kinds()

	expectedKinds := []string{"sh_binary", "helm_chart", "argocd_app"}

	for _, k := range expectedKinds {
		if _, ok := kinds[k]; !ok {
			t.Errorf("Kinds() missing kind %q", k)
		}
	}
}

func TestArgoCDLang_Kinds_ShBinary(t *testing.T) {
	lang := NewLanguage()
	kinds := lang.Kinds()

	shBinary, ok := kinds["sh_binary"]
	if !ok {
		t.Fatal("Kinds() missing sh_binary")
	}

	if !shBinary.NonEmptyAttrs["srcs"] {
		t.Error("sh_binary should have srcs as non-empty attr")
	}

	if !shBinary.MergeableAttrs["args"] {
		t.Error("sh_binary should have args as mergeable attr")
	}

	if !shBinary.MergeableAttrs["env"] {
		t.Error("sh_binary should have env as mergeable attr")
	}
}

func TestArgoCDLang_Kinds_HelmChart(t *testing.T) {
	lang := NewLanguage()
	kinds := lang.Kinds()

	helmChart, ok := kinds["helm_chart"]
	if !ok {
		t.Fatal("Kinds() missing helm_chart")
	}

	if !helmChart.NonEmptyAttrs["visibility"] {
		t.Error("helm_chart should have visibility as non-empty attr")
	}

	if !helmChart.MergeableAttrs["visibility"] {
		t.Error("helm_chart should have visibility as mergeable attr")
	}
}

func TestArgoCDLang_Kinds_ArgocdApp(t *testing.T) {
	lang := NewLanguage()
	kinds := lang.Kinds()

	argocdApp, ok := kinds["argocd_app"]
	if !ok {
		t.Fatal("Kinds() missing argocd_app")
	}

	// Check NonEmptyAttrs
	nonEmptyExpected := []string{"chart", "chart_files", "release_name", "namespace", "values_files"}
	for _, attr := range nonEmptyExpected {
		if !argocdApp.NonEmptyAttrs[attr] {
			t.Errorf("argocd_app should have %s as non-empty attr", attr)
		}
	}

	// Check MergeableAttrs
	mergeableExpected := []string{"values_files", "tags"}
	for _, attr := range mergeableExpected {
		if !argocdApp.MergeableAttrs[attr] {
			t.Errorf("argocd_app should have %s as mergeable attr", attr)
		}
	}
}

func TestArgoCDLang_Loads(t *testing.T) {
	lang := NewLanguage()
	loads := lang.Loads()

	if len(loads) != 2 {
		t.Errorf("Loads() returned %d loads, want 2", len(loads))
	}

	loadMap := make(map[string][]string)
	for _, l := range loads {
		loadMap[l.Name] = l.Symbols
	}

	// Check sh_binary load
	if symbols, ok := loadMap["@rules_shell//shell:sh_binary.bzl"]; !ok {
		t.Error("Loads() missing sh_binary.bzl")
	} else {
		found := false
		for _, s := range symbols {
			if s == "sh_binary" {
				found = true
				break
			}
		}
		if !found {
			t.Error("sh_binary.bzl should export sh_binary symbol")
		}
	}

	// Check defs.bzl load
	if symbols, ok := loadMap["//bazel/helm:defs.bzl"]; !ok {
		t.Error("Loads() missing defs.bzl")
	} else {
		foundHelmChart := false
		foundArgocdApp := false
		for _, s := range symbols {
			if s == "helm_chart" {
				foundHelmChart = true
			}
			if s == "argocd_app" {
				foundArgocdApp = true
			}
		}
		if !foundHelmChart {
			t.Error("defs.bzl should export helm_chart symbol")
		}
		if !foundArgocdApp {
			t.Error("defs.bzl should export argocd_app symbol")
		}
	}
}

func TestArgoCDLang_CheckFlags(t *testing.T) {
	lang := NewLanguage()
	err := lang.CheckFlags(nil, nil)
	if err != nil {
		t.Errorf("CheckFlags() returned error: %v", err)
	}
}

func TestArgoCDLang_Imports(t *testing.T) {
	lang := NewLanguage()
	imports := lang.Imports(nil, nil, nil)

	if imports != nil {
		t.Errorf("Imports() should return nil, got %v", imports)
	}
}

func TestArgoCDLang_Embeds(t *testing.T) {
	lang := &argoCDLang{}
	embeds := lang.Embeds(nil, label.Label{})

	if embeds != nil {
		t.Errorf("Embeds() should return nil, got %v", embeds)
	}
}
