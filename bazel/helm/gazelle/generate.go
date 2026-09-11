package gazelle

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/rule"
	"sigs.k8s.io/yaml"
)

// discoverOverlaysUsingChart finds all overlay packages that reference the given chart path.
// Returns a list of Bazel package paths (e.g., "//overlays/prod/n8n")
func discoverOverlaysUsingChart(workspaceRoot, chartPath string) []string {
	var overlayPaths []string

	overlaysDir := filepath.Join(workspaceRoot, "overlays")

	// Check if overlays directory exists
	if _, err := os.Stat(overlaysDir); os.IsNotExist(err) {
		return nil
	}

	// Walk the overlays directory to find all application.yaml files
	err := filepath.Walk(overlaysDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // Skip directories we can't access
		}

		// Only process application.yaml files
		if info.IsDir() || filepath.Base(path) != "application.yaml" {
			return nil
		}

		// Parse the application
		app, err := parseApplication(path)
		if err != nil {
			return nil // Skip files we can't parse
		}

		// Check if this application references our chart
		if app.Spec.Source.Path == chartPath {
			// Get the directory containing this application.yaml
			overlayDir := filepath.Dir(path)
			// Make path relative to workspace root
			relPath, err := filepath.Rel(workspaceRoot, overlayDir)
			if err != nil {
				return nil
			}
			// Convert to Bazel label format with __pkg__ to reference the package
			overlayPaths = append(overlayPaths, "//"+filepath.ToSlash(relPath)+":__pkg__")
		}

		return nil
	})
	if err != nil {
		return nil
	}

	return overlayPaths
}

// HelmChart represents the minimal structure of a Chart.yaml needed for Gazelle.
type HelmChart struct {
	Dependencies []struct {
		Name string `yaml:"name"`
	} `yaml:"dependencies"`
}

// chartHasDependencies checks if a Chart.yaml declares sub-chart dependencies.
// Charts with unresolved dependencies fail helm lint --strict, so we disable
// lint for them automatically.
func chartHasDependencies(chartYamlPath string) bool {
	data, err := os.ReadFile(chartYamlPath)
	if err != nil {
		return false
	}

	var chart HelmChart
	if err := yaml.Unmarshal(data, &chart); err != nil {
		return false
	}

	return len(chart.Dependencies) > 0
}

// ArgoCDApplication represents the structure of an ArgoCD Application manifest.
type ArgoCDApplication struct {
	APIVersion string `yaml:"apiVersion"`
	Kind       string `yaml:"kind"`
	Metadata   struct {
		Name      string `yaml:"name"`
		Namespace string `yaml:"namespace"`
	} `yaml:"metadata"`
	Spec struct {
		Source struct {
			RepoURL        string `yaml:"repoURL"`
			Path           string `yaml:"path"`
			Chart          string `yaml:"chart"` // For remote Helm charts
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
	} `yaml:"spec"`
}

// generateRules generates BUILD rules for ArgoCD applications and chart directories.
func generateRules(args language.GenerateArgs) language.GenerateResult {
	cfg := getArgoCDConfig(args.Config)

	var result language.GenerateResult

	// Only generate rules if enabled
	if !cfg.enabled {
		return result
	}

	// Check if this is a chart directory (has Chart.yaml)
	chartFile := filepath.Join(args.Dir, "Chart.yaml")
	if _, err := os.Stat(chartFile); err == nil {
		// This is a Helm chart directory - create a helm_chart() macro call
		helmChartRule := rule.NewRule("helm_chart", "chart")

		// Dynamically discover which overlays use this chart and set precise visibility
		chartPath := args.Rel
		overlays := discoverOverlaysUsingChart(args.Config.RepoRoot, chartPath)
		if len(overlays) > 0 {
			helmChartRule.SetAttr("visibility", overlays)
		} else {
			// Fallback to overlays if no specific overlays found (shouldn't happen normally)
			helmChartRule.SetAttr("visibility", []string{"//overlays:__subpackages__"})
		}

		// Disable lint for charts with unresolved dependencies (helm lint --strict
		// fails when declared dependencies aren't downloaded)
		if chartHasDependencies(chartFile) {
			helmChartRule.SetAttr("lint", false)
		}

		result.Gen = append(result.Gen, helmChartRule)
		result.Imports = append(result.Imports, nil)

		return result
	}

	// Look for application.yaml files
	applicationFile := filepath.Join(args.Dir, "application.yaml")
	if _, err := os.Stat(applicationFile); os.IsNotExist(err) {
		return result
	}

	// Parse the application.yaml
	app, err := parseApplication(applicationFile)
	if err != nil {
		// If we can't parse it, skip generation
		return result
	}

	// Only process ArgoCD Applications
	if app.Kind != "Application" || !strings.HasPrefix(app.APIVersion, "argoproj.io/") {
		return result
	}

	// Generate argocd_app for local Helm sources. The macro owns the optional
	// live diff target so hand-written and generated applications share exactly
	// the same rendering and runfiles contract.
	// Note: releaseName is optional in ArgoCD (defaults to app name if not specified)
	if app.Spec.Source.Path != "" {
		appRule := generateArgoCDAppRule(app, args.Rel, args.Dir, cfg)
		if appRule != nil {
			result.Gen = append(result.Gen, appRule)
			result.Imports = append(result.Imports, nil)
		}
	}

	return result
}

// parseApplication reads and parses an ArgoCD Application YAML file.
func parseApplication(path string) (*ArgoCDApplication, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var app ArgoCDApplication
	if err := yaml.Unmarshal(data, &app); err != nil {
		return nil, err
	}

	return &app, nil
}

// generateArgoCDAppRule creates an argocd_app rule that encapsulates chart rendering and testing.
func generateArgoCDAppRule(app *ArgoCDApplication, currentPackage string, currentDir string, cfg *argoCDConfig) *rule.Rule {
	chartPath := app.Spec.Source.Path
	if chartPath == "" {
		return nil
	}

	r := rule.NewRule("argocd_app", app.Metadata.Name)

	// Set chart path
	r.SetAttr("chart", chartPath)

	// Set chart_files label for dependencies
	r.SetAttr("chart_files", "//"+filepath.ToSlash(chartPath)+":chart")

	// releaseName defaults to app name if not specified (ArgoCD behavior)
	releaseName := app.Spec.Source.Helm.ReleaseName
	if releaseName == "" {
		releaseName = app.Metadata.Name
	}
	r.SetAttr("release_name", releaseName)

	// Set namespace
	r.SetAttr("namespace", app.Spec.Destination.Namespace)

	// Build the values_files list (as Bazel labels)
	var valuesFiles []string

	// Add chart's default values.yaml first
	valuesFiles = append(valuesFiles, "//"+filepath.ToSlash(chartPath)+":values.yaml")

	// Add all valueFiles referenced in the Application spec
	for _, vf := range app.Spec.Source.Helm.ValueFiles {
		if strings.HasPrefix(vf, "../") {
			// Relative path - resolve it relative to chart path
			resolvedPath := filepath.Join(chartPath, vf)
			cleanPath := filepath.Clean(resolvedPath)

			// Skip paths that escape workspace root
			if strings.HasPrefix(cleanPath, "..") {
				continue
			}

			// Check if this file is in the current package
			fileDir := filepath.Dir(cleanPath)
			fileName := filepath.Base(cleanPath)

			if filepath.ToSlash(fileDir) == currentPackage {
				// File is in current package, use relative reference
				valuesFiles = append(valuesFiles, fileName)
			} else {
				// File is in different package, use absolute label
				valuesFiles = append(valuesFiles, "//"+filepath.ToSlash(fileDir)+":"+fileName)
			}
		} else if !strings.Contains(vf, "/") {
			// Simple filename in chart directory
			valuesFiles = append(valuesFiles, "//"+filepath.ToSlash(chartPath)+":"+vf)
		}
	}

	// Deduplicate values files
	seen := make(map[string]bool)
	var uniqueValuesFiles []string
	for _, vf := range valuesFiles {
		if !seen[vf] {
			seen[vf] = true
			uniqueValuesFiles = append(uniqueValuesFiles, vf)
		}
	}

	r.SetAttr("values_files", uniqueValuesFiles)

	// Tag for filtering
	r.SetAttr("tags", []string{"helm", "template"})

	// generate_manifests defaults to true in the macro, so only set if false
	if !cfg.generateManifests {
		r.SetAttr("generate_manifests", false)
	}

	if cfg.generateDiff {
		r.SetAttr("generate_diff", true)
	}

	return r
}
