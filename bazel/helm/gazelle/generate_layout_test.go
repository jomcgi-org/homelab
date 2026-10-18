package gazelle

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/merger"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

func writeLayoutFile(t *testing.T, root, path, contents string) {
	t.Helper()
	path = filepath.Join(root, path)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}
}

func generateLayout(t *testing.T, root, pkg string, file *rule.File) language.GenerateResult {
	t.Helper()
	c := config.New()
	c.RepoRoot = root
	l := NewLanguage()
	l.Configure(c, pkg, file)
	return l.GenerateRules(language.GenerateArgs{
		Config: c,
		Dir:    filepath.Join(root, pkg),
		Rel:    pkg,
		File:   file,
	})
}

func TestGenerateRulesProjectLayouts(t *testing.T) {
	for _, tc := range []struct {
		name, chart, deploy string
		dependencies        bool
	}{
		{"ordinary service", "projects/widget/chart", "projects/widget/deploy", false},
		{"nested service", "projects/mcp/widget/chart", "projects/mcp/widget/deploy", true},
		{"cross-project consumer", "projects/widget/chart", "projects/validation/deploy", false},
		{"same-package consumer", "projects/platform/widget", "projects/platform/widget", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir() // Deliberately contains no overlays directory.
			chart := "apiVersion: v2\nname: widget\nversion: 1.0.0\n"
			if tc.dependencies {
				chart += "dependencies:\n  - name: upstream\n    version: 1.0.0\n    repository: https://example.com/charts\n"
			}
			writeLayoutFile(t, root, tc.chart+"/Chart.yaml", chart)
			writeLayoutFile(t, root, tc.chart+"/values.yaml", "replicaCount: 1\n")
			writeLayoutFile(t, root, tc.deploy+"/values.yaml", "replicaCount: 2\n")
			values, err := filepath.Rel(tc.chart, tc.deploy+"/values.yaml")
			if err != nil {
				t.Fatal(err)
			}
			writeLayoutFile(t, root, tc.deploy+"/application.yaml", fmt.Sprintf(`apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: widget
spec:
  source:
    path: %s
    helm:
      valueFiles:
        - values.yaml
        - %s
  destination:
    namespace: widgets
`, tc.chart, filepath.ToSlash(values)))

			// Same-package applications are hand-written today. Chart generation
			// must leave them in place when Gazelle merges the chart rule.
			build := ""
			if tc.chart == tc.deploy {
				build = fmt.Sprintf(`argocd_app(name = "widget", chart = %q, chart_files = %q, values_files = ["values.yaml"])`, tc.chart, "//"+tc.chart+":chart")
			}
			file, err := rule.LoadData(filepath.Join(root, tc.chart, "BUILD"), tc.chart, []byte(build))
			if err != nil {
				t.Fatal(err)
			}
			generated := generateLayout(t, root, tc.chart, file)
			if len(generated.Gen) != 1 || generated.Gen[0].Kind() != "helm_chart" {
				t.Fatalf("chart generation = %v, want one helm_chart", generated.Gen)
			}
			chartRule := generated.Gen[0]
			if got := chartRule.AttrStrings("visibility"); !reflect.DeepEqual(got, []string{"//visibility:public"}) {
				t.Fatalf("visibility = %v", got)
			}
			if tc.dependencies {
				if !reflect.DeepEqual(chartRule.Attr("lint"), rule.ExprFromValue(false)) {
					t.Fatal("unresolved chart dependencies must still disable lint")
				}
			} else if chartRule.Attr("lint") != nil {
				t.Fatal("dependency-free charts must keep default lint")
			}
			merger.MergeFile(file, generated.Empty, generated.Gen, merger.PreResolve, NewLanguage().Kinds(), nil)
			first := string(file.Format())
			generated = generateLayout(t, root, tc.chart, file)
			merger.MergeFile(file, generated.Empty, generated.Gen, merger.PreResolve, NewLanguage().Kinds(), nil)
			if string(file.Format()) != first {
				t.Fatal("second chart generation changed the BUILD file")
			}
			if tc.chart == tc.deploy {
				if len(file.Rules) != 2 || file.Rules[0].Kind() != "argocd_app" {
					t.Fatal("same-package application was lost or duplicated")
				}
				return
			}

			apps := generateLayout(t, root, tc.deploy, nil)
			if len(apps.Gen) != 1 || apps.Gen[0].Kind() != "argocd_app" {
				t.Fatalf("deploy generation = %v, want one argocd_app", apps.Gen)
			}
			app := apps.Gen[0]
			if app.AttrString("chart_files") != "//"+tc.chart+":chart" {
				t.Fatalf("chart consumer = %v", app.Attr("chart_files"))
			}
			wantValues := []string{"//" + tc.chart + ":values.yaml", "values.yaml"}
			if got := app.AttrStrings("values_files"); !reflect.DeepEqual(got, wantValues) {
				t.Fatalf("values_files = %v, want %v", got, wantValues)
			}
			if app.AttrString("namespace") != "widgets" || app.AttrString("release_name") != "widget" {
				t.Fatal("Application namespace or default release name changed")
			}
			if app.Attr("generate_manifests") != nil || app.Attr("generate_diff") != nil {
				t.Fatal("ordinary Application generation defaults changed")
			}
		})
	}
}

func TestGenerateRulesContextForge(t *testing.T) {
	const pkg = "projects/mcp/context-forge-gateway/deploy"
	root := filepath.Join("..", "..", "..")
	file, err := rule.LoadFile(filepath.Join(root, pkg, "BUILD"), pkg)
	if err != nil {
		t.Fatal(err)
	}
	generated := generateLayout(t, root, pkg, file)
	if len(generated.Gen) != 1 || generated.Gen[0].Kind() != "argocd_app" {
		t.Fatalf("Context Forge generation = %v, want one argocd_app", generated.Gen)
	}
	app := generated.Gen[0]
	if !reflect.DeepEqual(app.Attr("generate_manifests"), rule.ExprFromValue(false)) {
		t.Fatal("generated app must leave both admissibility renders intact")
	}
	wantValues := []string{"//projects/mcp/context-forge-gateway/chart:values.yaml", "values.yaml"}
	if got := app.AttrStrings("values_files"); !reflect.DeepEqual(got, wantValues) {
		t.Fatalf("Application values_files = %v, want %v", got, wantValues)
	}
	merger.MergeFile(file, generated.Empty, generated.Gen, merger.PreResolve, NewLanguage().Kinds(), nil)
	first := string(file.Format())
	generated = generateLayout(t, root, pkg, file)
	merger.MergeFile(file, generated.Empty, generated.Gen, merger.PreResolve, NewLanguage().Kinds(), nil)
	if string(file.Format()) != first {
		t.Fatal("Context Forge generation is not stable on the second pass")
	}
	counts := map[string]int{}
	for _, r := range file.Rules {
		counts[r.Kind()]++
	}
	if counts["argocd_app"] != 1 || counts["helm_admissibility_render"] != 2 || counts["filegroup"] != 3 {
		t.Fatalf("merged rule counts = %v", counts)
	}
}
