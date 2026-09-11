package gazelle

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseApplication(t *testing.T) {
	tests := []struct {
		name        string
		yamlContent string
		wantErr     bool
		validate    func(*testing.T, *ArgoCDApplication)
	}{
		{
			name: "valid application",
			yamlContent: `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: test-app
  namespace: argocd
spec:
  source:
    repoURL: https://github.com/example/repo.git
    path: charts/myapp
    targetRevision: HEAD
    helm:
      releaseName: my-release
      valueFiles:
        - values.yaml
        - values-prod.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: default
`,
			wantErr: false,
			validate: func(t *testing.T, app *ArgoCDApplication) {
				if app.APIVersion != "argoproj.io/v1alpha1" {
					t.Errorf("APIVersion = %q, want %q", app.APIVersion, "argoproj.io/v1alpha1")
				}
				if app.Kind != "Application" {
					t.Errorf("Kind = %q, want %q", app.Kind, "Application")
				}
				if app.Metadata.Name != "test-app" {
					t.Errorf("Metadata.Name = %q, want %q", app.Metadata.Name, "test-app")
				}
				if app.Metadata.Namespace != "argocd" {
					t.Errorf("Metadata.Namespace = %q, want %q", app.Metadata.Namespace, "argocd")
				}
				if app.Spec.Source.Path != "charts/myapp" {
					t.Errorf("Spec.Source.Path = %q, want %q", app.Spec.Source.Path, "charts/myapp")
				}
				if app.Spec.Source.Helm.ReleaseName != "my-release" {
					t.Errorf("Spec.Source.Helm.ReleaseName = %q, want %q", app.Spec.Source.Helm.ReleaseName, "my-release")
				}
				if len(app.Spec.Source.Helm.ValueFiles) != 2 {
					t.Errorf("len(ValueFiles) = %d, want 2", len(app.Spec.Source.Helm.ValueFiles))
				}
				if app.Spec.Destination.Namespace != "default" {
					t.Errorf("Spec.Destination.Namespace = %q, want %q", app.Spec.Destination.Namespace, "default")
				}
			},
		},
		{
			name: "application with remote chart",
			yamlContent: `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-app
  namespace: argocd
spec:
  source:
    repoURL: https://charts.bitnami.com/bitnami
    chart: nginx
    targetRevision: 15.0.0
  destination:
    server: https://kubernetes.default.svc
    namespace: nginx
`,
			wantErr: false,
			validate: func(t *testing.T, app *ArgoCDApplication) {
				if app.Spec.Source.Chart != "nginx" {
					t.Errorf("Spec.Source.Chart = %q, want %q", app.Spec.Source.Chart, "nginx")
				}
				if app.Spec.Source.TargetRevision != "15.0.0" {
					t.Errorf("Spec.Source.TargetRevision = %q, want %q", app.Spec.Source.TargetRevision, "15.0.0")
				}
			},
		},
		{
			name: "minimal application",
			yamlContent: `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: minimal
spec:
  source:
    path: charts/minimal
  destination:
    server: https://kubernetes.default.svc
`,
			wantErr: false,
			validate: func(t *testing.T, app *ArgoCDApplication) {
				if app.Metadata.Name != "minimal" {
					t.Errorf("Metadata.Name = %q, want %q", app.Metadata.Name, "minimal")
				}
				if app.Spec.Source.Path != "charts/minimal" {
					t.Errorf("Spec.Source.Path = %q, want %q", app.Spec.Source.Path, "charts/minimal")
				}
			},
		},
		{
			name:        "invalid yaml",
			yamlContent: `invalid: yaml: content: [`,
			wantErr:     true,
			validate:    nil,
		},
		{
			name:        "empty content",
			yamlContent: ``,
			wantErr:     false,
			validate: func(t *testing.T, app *ArgoCDApplication) {
				if app.Kind != "" {
					t.Errorf("Kind = %q, want empty", app.Kind)
				}
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			// Create a temp file with the YAML content
			tmpDir := t.TempDir()
			tmpFile := filepath.Join(tmpDir, "application.yaml")
			if err := os.WriteFile(tmpFile, []byte(tc.yamlContent), 0o644); err != nil {
				t.Fatalf("Failed to write temp file: %v", err)
			}

			app, err := parseApplication(tmpFile)

			if tc.wantErr {
				if err == nil {
					t.Error("expected error, got nil")
				}
				return
			}

			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if tc.validate != nil {
				tc.validate(t, app)
			}
		})
	}
}

func TestParseApplication_FileNotFound(t *testing.T) {
	_, err := parseApplication("/nonexistent/path/application.yaml")
	if err == nil {
		t.Error("expected error for nonexistent file, got nil")
	}
}

func TestChartHasDependencies(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    bool
	}{
		{
			name: "chart with dependencies",
			content: `apiVersion: v2
name: my-chart
dependencies:
  - name: subchart
    version: 1.0.0
    repository: https://example.com
`,
			want: true,
		},
		{
			name: "chart without dependencies",
			content: `apiVersion: v2
name: simple-chart
version: 0.1.0
`,
			want: false,
		},
		{
			name: "chart with empty dependencies",
			content: `apiVersion: v2
name: empty-deps
dependencies: []
`,
			want: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tmpDir := t.TempDir()
			chartFile := filepath.Join(tmpDir, "Chart.yaml")
			if err := os.WriteFile(chartFile, []byte(tc.content), 0o644); err != nil {
				t.Fatalf("Failed to write Chart.yaml: %v", err)
			}

			got := chartHasDependencies(chartFile)
			if got != tc.want {
				t.Errorf("chartHasDependencies() = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestChartHasDependencies_FileNotFound(t *testing.T) {
	got := chartHasDependencies("/nonexistent/Chart.yaml")
	if got != false {
		t.Error("expected false for nonexistent file")
	}
}

func TestDiscoverOverlaysUsingChart(t *testing.T) {
	// Create a temp workspace structure
	tmpDir := t.TempDir()

	// Create overlays directory structure
	overlaysDir := filepath.Join(tmpDir, "overlays", "prod", "myapp")
	if err := os.MkdirAll(overlaysDir, 0o755); err != nil {
		t.Fatalf("Failed to create overlays dir: %v", err)
	}

	// Create an application.yaml that references the chart
	appYaml := `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myapp
spec:
  source:
    path: charts/myapp
    helm:
      valueFiles:
        - values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: myapp
`
	if err := os.WriteFile(filepath.Join(overlaysDir, "application.yaml"), []byte(appYaml), 0o644); err != nil {
		t.Fatalf("Failed to write application.yaml: %v", err)
	}

	// Test discovery
	overlays := discoverOverlaysUsingChart(tmpDir, "charts/myapp")

	if len(overlays) != 1 {
		t.Fatalf("expected 1 overlay, got %d: %v", len(overlays), overlays)
	}

	expected := "//overlays/prod/myapp:__pkg__"
	if overlays[0] != expected {
		t.Errorf("overlay = %q, want %q", overlays[0], expected)
	}
}

func TestDiscoverOverlaysUsingChart_MultipleOverlays(t *testing.T) {
	tmpDir := t.TempDir()

	// Create multiple overlay environments
	envs := []string{"dev", "staging", "prod"}
	for _, env := range envs {
		overlaysDir := filepath.Join(tmpDir, "overlays", env, "myapp")
		if err := os.MkdirAll(overlaysDir, 0o755); err != nil {
			t.Fatalf("Failed to create overlays dir: %v", err)
		}

		appYaml := `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myapp
spec:
  source:
    path: charts/myapp
  destination:
    server: https://kubernetes.default.svc
`
		if err := os.WriteFile(filepath.Join(overlaysDir, "application.yaml"), []byte(appYaml), 0o644); err != nil {
			t.Fatalf("Failed to write application.yaml: %v", err)
		}
	}

	overlays := discoverOverlaysUsingChart(tmpDir, "charts/myapp")

	if len(overlays) != 3 {
		t.Fatalf("expected 3 overlays, got %d: %v", len(overlays), overlays)
	}
}

func TestDiscoverOverlaysUsingChart_NoMatches(t *testing.T) {
	tmpDir := t.TempDir()

	// Create an overlay that references a different chart
	overlaysDir := filepath.Join(tmpDir, "overlays", "prod", "other")
	if err := os.MkdirAll(overlaysDir, 0o755); err != nil {
		t.Fatalf("Failed to create overlays dir: %v", err)
	}

	appYaml := `apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: other
spec:
  source:
    path: charts/other
  destination:
    server: https://kubernetes.default.svc
`
	if err := os.WriteFile(filepath.Join(overlaysDir, "application.yaml"), []byte(appYaml), 0o644); err != nil {
		t.Fatalf("Failed to write application.yaml: %v", err)
	}

	overlays := discoverOverlaysUsingChart(tmpDir, "charts/myapp")

	if len(overlays) != 0 {
		t.Errorf("expected 0 overlays, got %d: %v", len(overlays), overlays)
	}
}

func TestDiscoverOverlaysUsingChart_NoOverlaysDir(t *testing.T) {
	tmpDir := t.TempDir()

	overlays := discoverOverlaysUsingChart(tmpDir, "charts/myapp")

	if overlays != nil {
		t.Errorf("expected nil, got %v", overlays)
	}
}

func TestDiscoverOverlaysUsingChart_InvalidYaml(t *testing.T) {
	tmpDir := t.TempDir()

	overlaysDir := filepath.Join(tmpDir, "overlays", "prod", "broken")
	if err := os.MkdirAll(overlaysDir, 0o755); err != nil {
		t.Fatalf("Failed to create overlays dir: %v", err)
	}

	// Write invalid YAML
	if err := os.WriteFile(filepath.Join(overlaysDir, "application.yaml"), []byte("invalid: yaml: ["), 0o644); err != nil {
		t.Fatalf("Failed to write application.yaml: %v", err)
	}

	// Should not panic and should return empty
	overlays := discoverOverlaysUsingChart(tmpDir, "charts/myapp")

	if len(overlays) != 0 {
		t.Errorf("expected 0 overlays for invalid yaml, got %d", len(overlays))
	}
}

func TestGenerateArgoCDAppRuleEnablesLiveDiff(t *testing.T) {
	app := &ArgoCDApplication{
		Metadata: struct {
			Name      string `yaml:"name"`
			Namespace string `yaml:"namespace"`
		}{
			Name:      "test-app",
			Namespace: "argocd",
		},
		Spec: struct {
			Source struct {
				RepoURL        string `yaml:"repoURL"`
				Path           string `yaml:"path"`
				Chart          string `yaml:"chart"`
				TargetRevision string `yaml:"targetRevision"`
				Helm           struct {
					ReleaseName string   `yaml:"releaseName"`
					ValueFiles  []string `yaml:"valueFiles"`
				} `yaml:"helm"`
			} `yaml:"source"`
			Destination struct {
				Server    string `yaml:"server"`
				Namespace string `yaml:"namespace"`
			} `yaml:"destination"`
		}{},
	}
	app.Spec.Source.Path = "projects/test/chart"
	app.Spec.Source.Helm.ValueFiles = []string{"../deploy/values.yaml"}
	app.Spec.Destination.Namespace = "test-ns"

	// Create temp dir with values.yaml
	tmpDir := t.TempDir()
	valuesFile := filepath.Join(tmpDir, "values.yaml")
	if err := os.WriteFile(valuesFile, []byte("key: value"), 0o644); err != nil {
		t.Fatalf("Failed to write values.yaml: %v", err)
	}

	cfg := &argoCDConfig{
		generateManifests: true,
		generateDiff:      true,
	}
	rule := generateArgoCDAppRule(app, "projects/test/deploy", tmpDir, cfg)

	if rule == nil {
		t.Fatal("generateArgoCDAppRule returned nil")
	}

	if rule.Kind() != "argocd_app" {
		t.Errorf("rule kind = %q, want %q", rule.Kind(), "argocd_app")
	}

	if rule.Name() != "test-app" {
		t.Errorf("rule name = %q, want %q", rule.Name(), "test-app")
	}

	if got := rule.Attr("generate_diff"); got == nil {
		t.Error("generate_diff is not set")
	}

	values := rule.AttrStrings("values_files")
	wantValues := []string{"//projects/test/chart:values.yaml", "values.yaml"}
	if len(values) != len(wantValues) {
		t.Fatalf("values_files = %v, want %v", values, wantValues)
	}
	for i := range wantValues {
		if values[i] != wantValues[i] {
			t.Errorf("values_files[%d] = %q, want %q", i, values[i], wantValues[i])
		}
	}
}

func TestArgoCDApplication_Structure(t *testing.T) {
	app := ArgoCDApplication{
		APIVersion: "argoproj.io/v1alpha1",
		Kind:       "Application",
	}
	app.Metadata.Name = "test"
	app.Metadata.Namespace = "argocd"
	app.Spec.Source.RepoURL = "https://github.com/example/repo"
	app.Spec.Source.Path = "charts/app"
	app.Spec.Source.Chart = "external-chart"
	app.Spec.Source.TargetRevision = "HEAD"
	app.Spec.Source.Helm.ReleaseName = "my-release"
	app.Spec.Source.Helm.ValueFiles = []string{"values.yaml"}
	app.Spec.Destination.Server = "https://kubernetes.default.svc"
	app.Spec.Destination.Namespace = "default"

	if app.APIVersion != "argoproj.io/v1alpha1" {
		t.Error("failed to set APIVersion")
	}
	if app.Kind != "Application" {
		t.Error("failed to set Kind")
	}
	if app.Metadata.Name != "test" {
		t.Error("failed to set Metadata.Name")
	}
	if app.Spec.Source.Chart != "external-chart" {
		t.Error("failed to set Spec.Source.Chart")
	}
}
