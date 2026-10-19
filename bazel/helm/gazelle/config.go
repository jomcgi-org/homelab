package gazelle

import (
	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

// argoCDConfig holds configuration for the ArgoCD Gazelle extension.
type argoCDConfig struct {
	// enabled controls whether to generate rules in this directory
	enabled bool
	// generateManifests controls whether to generate pre-rendered manifest targets
	generateManifests bool
}

const argoCDConfigKey = "argocd_config"

// getArgoCDConfig retrieves the ArgoCD configuration from a Bazel config.
func getArgoCDConfig(c *config.Config) *argoCDConfig {
	if cfg, ok := c.Exts[argoCDConfigKey].(*argoCDConfig); ok {
		return cfg
	}
	// Default configuration
	return &argoCDConfig{
		enabled:           true, // Enabled by default
		generateManifests: true, // Enabled by default for pre-rendered manifests
	}
}

// configure reads ArgoCD-specific directives from BUILD files.
func configure(c *config.Config, rel string, f *rule.File) {
	// Start with the parent directory's configuration
	parent := getArgoCDConfig(c)

	// Clone the parent configuration
	cfg := &argoCDConfig{
		enabled:           parent.enabled,
		generateManifests: parent.generateManifests,
	}

	// Process directives if a BUILD file exists
	if f != nil {
		for _, d := range f.Directives {
			switch d.Key {
			case "argocd":
				// # gazelle:argocd enabled
				// # gazelle:argocd disabled
				if len(d.Value) > 0 {
					cfg.enabled = d.Value == "enabled"
				}

			case "argocd_enabled":
				// # gazelle:argocd_enabled
				cfg.enabled = true

			case "argocd_generate_manifests":
				// # gazelle:argocd_generate_manifests true   (enabled by default)
				// # gazelle:argocd_generate_manifests false  (opt-out)
				cfg.generateManifests = d.Value == "true"
			}
		}
	}

	// Store the configuration
	c.Exts[argoCDConfigKey] = cfg
}
