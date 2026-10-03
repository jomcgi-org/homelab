// Package gazelle provides a Gazelle extension for auto-generating BUILD files
// from ArgoCD Application manifests.
package gazelle

import (
	"flag"

	"github.com/bazelbuild/bazel-gazelle/config"
	"github.com/bazelbuild/bazel-gazelle/label"
	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/repo"
	"github.com/bazelbuild/bazel-gazelle/resolve"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

const languageName = "argocd"

// NewLanguage creates a new instance of the ArgoCD Gazelle language.
func NewLanguage() language.Language {
	return &argoCDLang{}
}

type argoCDLang struct{}

// Name returns the name of this language extension.
func (l *argoCDLang) Name() string {
	return languageName
}

// RegisterFlags registers command-line flags for this extension.
func (l *argoCDLang) RegisterFlags(fs *flag.FlagSet, cmd string, c *config.Config) {
	// No custom flags needed yet
}

// CheckFlags validates command-line flags.
func (l *argoCDLang) CheckFlags(fs *flag.FlagSet, c *config.Config) error {
	return nil
}

// KnownDirectives returns the list of directives that this extension recognizes.
func (l *argoCDLang) KnownDirectives() []string {
	return []string{
		"argocd",
		"argocd_enabled",
		"argocd_generate_diff",
		"argocd_generate_manifests",
		"kubectl_context",
	}
}

// Configure reads configuration from directives and command-line flags.
func (l *argoCDLang) Configure(c *config.Config, rel string, f *rule.File) {
	configure(c, rel, f)
}

// Kinds returns the list of rule kinds that this extension can generate.
func (l *argoCDLang) Kinds() map[string]rule.KindInfo {
	return map[string]rule.KindInfo{
		"filegroup": {
			NonEmptyAttrs: map[string]bool{
				"srcs": true,
			},
			MergeableAttrs: map[string]bool{
				"srcs":       true,
				"visibility": true,
			},
		},
		"sh_binary": {
			MatchAny: false,
			NonEmptyAttrs: map[string]bool{
				"srcs": true,
			},
			MergeableAttrs: map[string]bool{
				"args": true,
				"env":  true,
			},
		},
		"helm_chart": {
			MatchAny: false,
			NonEmptyAttrs: map[string]bool{
				"visibility": true,
			},
			MergeableAttrs: map[string]bool{
				"visibility": true,
			},
		},
		"argocd_app": {
			MatchAny: false,
			NonEmptyAttrs: map[string]bool{
				"chart":        true,
				"chart_files":  true,
				"release_name": true,
				"namespace":    true,
				"values_files": true,
			},
			MergeableAttrs: map[string]bool{
				"values_files": true,
				"tags":         true,
			},
		},
	}
}

// Loads returns the list of labels that this extension may load.
func (l *argoCDLang) Loads() []rule.LoadInfo {
	return []rule.LoadInfo{
		{
			Name:    "@rules_shell//shell:sh_binary.bzl",
			Symbols: []string{"sh_binary"},
		},
		{
			Name:    "//bazel/helm:defs.bzl",
			Symbols: []string{"helm_chart", "argocd_app"},
		},
	}
}

// GenerateRules generates Bazel rules for ArgoCD applications.
func (l *argoCDLang) GenerateRules(args language.GenerateArgs) language.GenerateResult {
	return generateRules(args)
}

// Fix applies automated fixes to existing rules.
func (l *argoCDLang) Fix(c *config.Config, f *rule.File) {
	// No automated fixes needed yet
}

// Imports extracts import information from rules (not applicable for ArgoCD).
func (l *argoCDLang) Imports(c *config.Config, r *rule.Rule, f *rule.File) []resolve.ImportSpec {
	return nil
}

// Embeds extracts embed information from rules (not applicable for ArgoCD).
func (l *argoCDLang) Embeds(r *rule.Rule, from label.Label) []label.Label {
	return nil
}

// Resolve translates import paths to Bazel labels (not applicable for ArgoCD).
func (l *argoCDLang) Resolve(c *config.Config, ix *resolve.RuleIndex, rc *repo.RemoteCache, r *rule.Rule, imports interface{}, from label.Label) {
	// No resolution needed
}
