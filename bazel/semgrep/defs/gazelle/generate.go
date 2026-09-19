package gazelle

import (
	"path/filepath"
	"sort"
	"strings"

	"github.com/bazelbuild/bazel-gazelle/language"
	"github.com/bazelbuild/bazel-gazelle/rule"
)

// libraryKinds are the rule kinds that represent Python libraries.
var libraryKinds = map[string]bool{
	"py_library": true,
}

// generateRules generates semgrep test targets for a package.
//
// When configured target kinds exist in the BUILD file:
//   - Generates one semgrep_target_test per target (aspect scans transitive deps)
//   - For self-targeting kinds (attr=""), also walks local deps to find covered files
//   - Deduplicates by resolved target label
//   - Generates per-file semgrep_test only for orphan files not covered by any
//     target's transitive local deps (files that the aspect won't scan)
//
// When no configured targets exist, falls back to per-file semgrep_test for
// every scannable file.
func generateRules(args language.GenerateArgs) language.GenerateResult {
	cfg := getSemgrepConfig(args.Config)

	var result language.GenerateResult

	if !cfg.enabled {
		return result
	}

	scanFiles := scannableFiles(args.RegularFiles, cfg.languages)

	// Detect configured targets in the existing BUILD file
	targets := findTargets(args.File, cfg.targetKinds)

	if len(scanFiles) == 0 && len(targets) == 0 {
		result.Empty = staleRules(args, nil)
		return result
	}

	if len(targets) > 0 {
		// Build the set of files covered by self-targeting kinds' transitive local deps.
		coveredFiles := coveredByTargets(args.File, targets, cfg.targetKinds)

		// Resolve each target and deduplicate by resolved label.
		// Track which canonical labels we've already emitted.
		seen := make(map[string]int)

		// Collect target tests, sorted by rule name for determinism
		type targetEntry struct {
			name   string
			target string
			rule   *rule.Rule // original BUILD rule for dep inspection
		}
		var entries []targetEntry
		for _, t := range targets {
			resolved := resolveTarget(t, cfg.targetKinds)
			canonical := canonicalTargetLabel(resolved, args.Rel)
			entry := targetEntry{
				name:   t.Name() + "_semgrep_test",
				target: resolved,
				rule:   t,
			}
			if i, ok := seen[canonical]; ok {
				// Prefer a self-targeting rule because its deps provide lockfile
				// and SCA metadata that an indirect wrapper may not expose.
				if cfg.targetKinds[entries[i].rule.Kind()] != "" && cfg.targetKinds[t.Kind()] == "" {
					entries[i] = entry
				}
				continue
			}
			seen[canonical] = len(entries)
			entries = append(entries, entry)
		}
		sort.Slice(entries, func(i, j int) bool {
			return entries[i].name < entries[j].name
		})

		allRules := rulesForLanguages(cfg.languages)

		for _, e := range entries {
			r := rule.NewRule("semgrep_target_test", e.name)
			r.SetAttr("target", e.target)
			r.SetAttr("rules", allRules)
			if len(cfg.excludeRules) > 0 {
				r.SetAttr("exclude_rules", sortedExcludeRules(cfg.excludeRules))
			}
			// Detect lockfiles from target deps
			if cfg.scaEnabled {
				lockfiles := detectLockfiles(e.rule, cfg)
				if len(lockfiles) > 0 {
					r.SetAttr("lockfiles", lockfiles)
					scaRules := detectScaRules(e.rule, cfg)
					if len(scaRules) > 0 {
						r.SetAttr("sca_rules", scaRules)
					}
				}
			}
			result.Gen = append(result.Gen, r)
			result.Imports = append(result.Imports, nil)
		}

		// Generate per-file semgrep_test for orphan files
		for _, f := range scanFiles {
			if coveredFiles[f] {
				continue
			}
			ext := fileExtension(f)
			name := strings.TrimSuffix(f, ext) + "_semgrep_test"
			r := rule.NewRule("semgrep_test", name)
			r.SetAttr("srcs", []string{f})
			r.SetAttr("rules", rulesForExtension(ext, cfg.languages))
			if len(cfg.excludeRules) > 0 {
				r.SetAttr("exclude_rules", sortedExcludeRules(cfg.excludeRules))
			}
			result.Gen = append(result.Gen, r)
			result.Imports = append(result.Imports, nil)
		}
	} else {
		// No configured targets — fall back to per-file semgrep_test
		for _, f := range scanFiles {
			ext := fileExtension(f)
			name := strings.TrimSuffix(f, ext) + "_semgrep_test"
			r := rule.NewRule("semgrep_test", name)
			r.SetAttr("srcs", []string{f})
			r.SetAttr("rules", rulesForExtension(ext, cfg.languages))
			if len(cfg.excludeRules) > 0 {
				r.SetAttr("exclude_rules", sortedExcludeRules(cfg.excludeRules))
			}
			result.Gen = append(result.Gen, r)
			result.Imports = append(result.Imports, nil)
		}
	}

	result.Empty = staleRules(args, result.Gen)

	return result
}

// coveredByTargets returns the set of files that are transitively reachable
// from self-targeting kinds through package-local deps. Indirected kinds
// (e.g. py3_image=binary) point to cross-package targets, so local dep
// walking doesn't apply to them.
func coveredByTargets(f *rule.File, targets []*rule.Rule, targetKinds map[string]string) map[string]bool {
	if f == nil {
		return nil
	}

	// Collect only self-targeting rules (attr == "")
	var selfTargets []*rule.Rule
	for _, t := range targets {
		if attr := targetKinds[t.Kind()]; attr == "" {
			selfTargets = append(selfTargets, t)
		}
	}

	if len(selfTargets) == 0 {
		return nil
	}

	// Index: target name -> rule
	ruleByName := make(map[string]*rule.Rule)
	for _, r := range f.Rules {
		ruleByName[r.Name()] = r
	}

	// Index: target name -> srcs (for self-targeting kinds and libraries)
	srcsByName := make(map[string][]string)
	for _, r := range f.Rules {
		kind := r.Kind()
		if _, isSelfTarget := targetKinds[kind]; (isSelfTarget && targetKinds[kind] == "") || libraryKinds[kind] {
			srcsByName[r.Name()] = r.AttrStrings("srcs")
		}
	}

	covered := make(map[string]bool)
	visited := make(map[string]bool)

	for _, t := range selfTargets {
		walkLocalDeps(t, ruleByName, srcsByName, covered, visited)
	}

	return covered
}

// walkLocalDeps recursively collects srcs from a target and its
// package-local dependencies.
func walkLocalDeps(r *rule.Rule, ruleByName map[string]*rule.Rule, srcsByName map[string][]string, covered map[string]bool, visited map[string]bool) {
	name := r.Name()
	if visited[name] {
		return
	}
	visited[name] = true

	// Mark this target's srcs as covered
	if main := r.AttrString("main"); main != "" {
		covered[main] = true
	}
	for _, src := range srcsByName[name] {
		covered[src] = true
	}

	// Walk local deps (":foo" references)
	for _, dep := range r.AttrStrings("deps") {
		localName := localDepName(dep)
		if localName == "" {
			continue
		}
		depRule, ok := ruleByName[localName]
		if !ok {
			continue
		}
		walkLocalDeps(depRule, ruleByName, srcsByName, covered, visited)
	}
}

// localDepName extracts the target name from a package-local dep like ":foo".
// Returns "" for external or cross-package deps.
func localDepName(dep string) string {
	if strings.HasPrefix(dep, ":") {
		return dep[1:]
	}
	return ""
}

// findTargets returns rules matching configured targetKinds from the BUILD file,
// sorted by name for deterministic output.
func findTargets(f *rule.File, targetKinds map[string]string) []*rule.Rule {
	if f == nil {
		return nil
	}
	var targets []*rule.Rule
	for _, r := range f.Rules {
		if _, ok := targetKinds[r.Kind()]; ok {
			targets = append(targets, r)
		}
	}
	// Sort by name for deterministic output
	sort.Slice(targets, func(i, j int) bool {
		return targets[i].Name() < targets[j].Name()
	})
	return targets
}

// resolveTarget returns the label that a rule resolves to for semgrep scanning.
// For self-targeting kinds (attr=""), returns ":name".
// For indirected kinds (attr="binary"), reads that attr value.
func resolveTarget(r *rule.Rule, targetKinds map[string]string) string {
	attr := targetKinds[r.Kind()]
	if attr == "" {
		return ":" + r.Name()
	}
	return r.AttrString(attr)
}

// canonicalTargetLabel returns an absolute label for package-relative targets.
// This lets deduplication treat :name and //package:name as the same target.
func canonicalTargetLabel(target, pkg string) string {
	if strings.HasPrefix(target, ":") {
		return "//" + pkg + target
	}
	return target
}

// scannableFiles returns the sorted subset of files with extensions matching
// the configured languages.
func scannableFiles(files []string, languages []string) []string {
	extSet := make(map[string]bool)
	for _, lang := range languages {
		if ext, ok := langExtensions[lang]; ok {
			extSet[ext] = true
		}
	}

	var scannable []string
	for _, f := range files {
		if extSet[fileExtension(f)] {
			scannable = append(scannable, f)
		}
	}
	sort.Strings(scannable)
	return scannable
}

// fileExtension returns the file extension including the dot (e.g. ".py").
func fileExtension(f string) string {
	return filepath.Ext(f)
}

// rulesForLanguages returns sorted rule config labels for all configured languages.
func rulesForLanguages(languages []string) []string {
	var rules []string
	for _, lang := range languages {
		if label, ok := langRules[lang]; ok {
			rules = append(rules, label)
		}
	}
	sort.Strings(rules)
	return rules
}

// rulesForExtension returns rule config labels for a specific file extension.
func rulesForExtension(ext string, languages []string) []string {
	var rules []string
	for _, lang := range languages {
		if langExtensions[lang] == ext {
			if label, ok := langRules[lang]; ok {
				rules = append(rules, label)
			}
		}
	}
	sort.Strings(rules)
	return rules
}

// staleRules returns empty rules for existing semgrep_test and semgrep_target_test
// rules not in the generated set.
func staleRules(args language.GenerateArgs, gen []*rule.Rule) []*rule.Rule {
	if args.File == nil {
		return nil
	}

	active := make(map[string]bool)
	for _, r := range gen {
		active[r.Kind()+"/"+r.Name()] = true
	}

	var empty []*rule.Rule
	for _, r := range args.File.Rules {
		kind := r.Kind()
		if (kind == "semgrep_test" || kind == "semgrep_target_test") && !active[kind+"/"+r.Name()] {
			empty = append(empty, rule.NewRule(kind, r.Name()))
		}
	}
	return empty
}

// sortedExcludeRules returns a sorted copy of the exclude rules list.
func sortedExcludeRules(rules []string) []string {
	sorted := append([]string{}, rules...)
	sort.Strings(sorted)
	return sorted
}
