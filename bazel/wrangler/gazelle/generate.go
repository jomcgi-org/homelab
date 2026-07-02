package gazelle

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

// wranglerJSONC represents the minimal structure of a wrangler.jsonc file.
type wranglerJSONC struct {
	Name string `json:"name"`
}

// stripJSONComments removes full-line // comments from JSONC content.
var jsonCommentRegex = regexp.MustCompile(`(?m)^\s*//.*$`)

// parseWranglerJSONC reads a wrangler.jsonc file and extracts the project name.
func parseWranglerJSONC(path string) (*wranglerJSONC, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	// Strip full-line // comments to convert JSONC to JSON
	cleaned := jsonCommentRegex.ReplaceAll(data, nil)

	var cfg wranglerJSONC
	if err := json.Unmarshal(cleaned, &cfg); err != nil {
		return nil, err
	}

	return &cfg, nil
}

// deriveTargetName derives a Bazel target name from a directory path.
// Example: "websites/trips.jomcgi.dev" → "trips", "websites/jomcgi.dev" → "jomcgi"
func deriveTargetName(rel string) string {
	base := filepath.Base(rel)
	parts := strings.SplitN(base, ".", 2)
	return parts[0]
}

// generateRules generates wrangler_pages BUILD rules for directories containing wrangler.jsonc.
func generateRules(args language.GenerateArgs) language.GenerateResult {
	cfg := getWranglerConfig(args.Config)

	var result language.GenerateResult

	// Only generate rules if enabled
	if !cfg.enabled {
		return result
	}

	// Look for wrangler.jsonc in this directory
	wranglerFile := filepath.Join(args.Dir, "wrangler.jsonc")
	if _, err := os.Stat(wranglerFile); os.IsNotExist(err) {
		return result
	}

	// Parse the wrangler.jsonc to get the project name
	wranglerCfg, err := parseWranglerJSONC(wranglerFile)
	if err != nil {
		return result
	}

	if wranglerCfg.Name == "" {
		return result
	}

	// Generate wrangler_pages rule
	targetName := deriveTargetName(args.Rel)
	r := rule.NewRule("wrangler_pages", targetName)
	r.SetAttr("dist", cfg.dist)
	r.SetAttr("project_name", wranglerCfg.Name)
	r.SetAttr("wrangler", ":wrangler")
	r.SetAttr("visibility", []string{"//visibility:private"})

	result.Gen = append(result.Gen, r)
	result.Imports = append(result.Imports, nil)

	return result
}
