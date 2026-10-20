package gazelle

import (
	"testing"

	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

func TestGetArgoCDConfig_Defaults(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	cfg := getArgoCDConfig(c)

	if !cfg.enabled {
		t.Error("expected enabled to be true by default")
	}
	if !cfg.generateManifests {
		t.Error("expected generateManifests to be true by default")
	}
}

func TestGetArgoCDConfig_FromExtension(t *testing.T) {
	expected := &argoCDConfig{
		enabled:           false,
		generateManifests: false,
	}

	c := &config.Config{
		Exts: map[string]interface{}{
			argoCDConfigKey: expected,
		},
	}

	cfg := getArgoCDConfig(c)

	if cfg != expected {
		t.Error("expected to return the stored config")
	}
	if cfg.enabled != expected.enabled {
		t.Errorf("expected enabled=%v, got %v", expected.enabled, cfg.enabled)
	}
	if cfg.generateManifests != expected.generateManifests {
		t.Errorf("expected generateManifests=%v, got %v", expected.generateManifests, cfg.generateManifests)
	}
}

func TestConfigure_Directives(t *testing.T) {
	tests := []struct {
		name              string
		directives        []rule.Directive
		parentConfig      *argoCDConfig
		wantEnabled       bool
		wantGenerateManif bool
	}{
		{
			name:              "no directives uses defaults",
			directives:        nil,
			parentConfig:      nil,
			wantEnabled:       true,
			wantGenerateManif: true,
		},
		{
			name: "argocd enabled directive",
			directives: []rule.Directive{
				{Key: "argocd", Value: "enabled"},
			},
			parentConfig:      nil,
			wantEnabled:       true,
			wantGenerateManif: true,
		},
		{
			name: "argocd disabled directive",
			directives: []rule.Directive{
				{Key: "argocd", Value: "disabled"},
			},
			parentConfig:      nil,
			wantEnabled:       false,
			wantGenerateManif: true,
		},
		{
			name: "argocd_enabled directive",
			directives: []rule.Directive{
				{Key: "argocd_enabled", Value: ""},
			},
			parentConfig: &argoCDConfig{
				enabled:           false,
				generateManifests: true,
			},
			wantEnabled:       true,
			wantGenerateManif: true,
		},
		{
			name: "argocd_generate_manifests true overrides parent",
			directives: []rule.Directive{
				{Key: "argocd_generate_manifests", Value: "true"},
			},
			parentConfig:      &argoCDConfig{enabled: true, generateManifests: false},
			wantEnabled:       true,
			wantGenerateManif: true,
		},
		{
			name: "empty argocd directive preserves disabled parent",
			directives: []rule.Directive{
				{Key: "argocd", Value: ""},
			},
			parentConfig:      &argoCDConfig{enabled: false, generateManifests: false},
			wantEnabled:       false,
			wantGenerateManif: false,
		},
		{
			name: "argocd_generate_manifests false",
			directives: []rule.Directive{
				{Key: "argocd_generate_manifests", Value: "false"},
			},
			parentConfig:      nil,
			wantEnabled:       true,
			wantGenerateManif: false,
		},
		{
			name:              "nil file inherits disabled parent",
			parentConfig:      &argoCDConfig{enabled: false, generateManifests: false},
			wantEnabled:       false,
			wantGenerateManif: false,
		},
		{
			name: "multiple directives",
			directives: []rule.Directive{
				{Key: "argocd", Value: "disabled"},
				{Key: "argocd_generate_manifests", Value: "false"},
			},
			parentConfig:      nil,
			wantEnabled:       false,
			wantGenerateManif: false,
		},
		{
			name: "inherits from parent",
			directives: []rule.Directive{
				{Key: "argocd", Value: "enabled"},
			},
			parentConfig: &argoCDConfig{
				enabled:           false,
				generateManifests: false,
			},
			wantEnabled:       true,
			wantGenerateManif: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c := &config.Config{
				Exts: make(map[string]interface{}),
			}

			if tc.parentConfig != nil {
				c.Exts[argoCDConfigKey] = tc.parentConfig
			}

			var f *rule.File
			if tc.directives != nil {
				f = &rule.File{
					Directives: tc.directives,
				}
			}

			var parentBefore argoCDConfig
			if tc.parentConfig != nil {
				parentBefore = *tc.parentConfig
			}
			configure(c, "", f)

			cfg := c.Exts[argoCDConfigKey].(*argoCDConfig)

			if cfg.enabled != tc.wantEnabled {
				t.Errorf("enabled: got %v, want %v", cfg.enabled, tc.wantEnabled)
			}
			if cfg.generateManifests != tc.wantGenerateManif {
				t.Errorf("generateManifests: got %v, want %v", cfg.generateManifests, tc.wantGenerateManif)
			}
			if tc.parentConfig != nil && (cfg == tc.parentConfig || *tc.parentConfig != parentBefore) {
				t.Error("configure must clone the parent without mutating it")
			}
		})
	}
}

func TestConfigure_NilFile(t *testing.T) {
	c := &config.Config{
		Exts: make(map[string]interface{}),
	}

	configure(c, "some/rel/path", nil)

	cfg := c.Exts[argoCDConfigKey].(*argoCDConfig)

	if !cfg.enabled {
		t.Error("expected enabled to be true with nil file")
	}
	if !cfg.generateManifests {
		t.Error("expected manifests to be enabled with nil file")
	}
}
