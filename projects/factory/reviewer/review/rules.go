package review

import (
	"context"
	"errors"
	"fmt"
	"path"
	"slices"
	"sort"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

// Rule file names. A root REVIEW.md takes over from FallbackRules once it
// exists.
const (
	RulesFile     = "REVIEW.md"
	FallbackRules = "docs/agents/review.md"
)

// Bounds on the rules a prompt carries. MaxRuleFiles bounds the rule files
// read; a tree read finds which exist, so it is reached only by a PR that
// touches as many directories holding a REVIEW.md.
const (
	MaxRuleFiles = 100
	maxRuleFile  = 16 << 10
	maxRules     = 64 << 10
)

// RuleFiles lists the REVIEW.md files that may apply to a change: the root
// one and one in every ancestor directory of each changed path (and of a
// rename's previous path). The root comes first, then by depth and name, so
// nearer files come later. A path no rule can be looked up for (an empty or
// ".." segment) is counted as unchecked.
func RuleFiles(paths []string) (files []string, unchecked int) {
	set := map[string]bool{RulesFile: true}
	for _, p := range paths {
		c, ok := lookupPath(p)
		if !ok {
			unchecked++
			continue
		}
		for dir := path.Dir(c); dir != "." && dir != "/"; dir = path.Dir(dir) {
			set[dir+"/"+RulesFile] = true
		}
	}
	for f := range set {
		files = append(files, f)
	}
	sort.Slice(files, func(i, j int) bool {
		di, dj := strings.Count(files[i], "/"), strings.Count(files[j], "/")
		if di != dj {
			return di < dj
		}
		return files[i] < files[j]
	})
	return files, unchecked
}

// Rules are the review rules a PR is subject to, and the repository context
// the agent reads them with, all read from the base branch.
type Rules struct {
	Base   string
	Commit string
	// Files are the rule files, root first; a file's includes come just
	// before it.
	Files []RuleFile
	// Context are the context documents, such as AGENTS.md.
	Context []RuleFile
	// Fallback is set when the base has no root REVIEW.md and FallbackRules
	// stands in for it.
	Fallback bool
	// Unchecked counts rule files past MaxRuleFiles and changed paths no
	// rule could be looked up for.
	Unchecked int
	// Notes report includes that are missing, malformed or loop.
	Notes []string
}

// RuleFile is one file's content, clipped to the prompt's bounds.
type RuleFile struct {
	Path    string
	Content string
	// Omitted is set when the file was too large to read inline or past the
	// prompt's budget; the agent reads it in its checkout of the base.
	Omitted bool
	Clipped bool
}

// RulesReader is the part of the GitHub client rules are read with.
type RulesReader interface {
	BranchHead(ctx context.Context, repo, branch string) (string, error)
	Tree(ctx context.Context, repo, ref string) (*github.Tree, error)
	FileContent(ctx context.Context, repo, path, ref string) ([]byte, error)
}

// Include bounds.
const (
	maxIncludeDepth = 5
	maxIncludesEach = 10
	maxContext      = 48 << 10
	maxContextDoc   = 24 << 10
)

// includes reads a rule file's "Includes: a, b" line, which must be its
// first non-blank line.
func includes(content string) []string {
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		rest, ok := strings.CutPrefix(line, "Includes:")
		if !ok {
			return nil
		}
		var out []string
		for _, p := range strings.Split(rest, ",") {
			if p = strings.TrimSpace(p); p != "" {
				out = append(out, p)
			}
		}
		return out
	}
	return nil
}

// LoadRules reads, from the base branch's current commit and never from the
// PR head, the REVIEW.md files that apply to the changed paths with the
// files they include, and the context documents. A PR cannot change the
// rules it is reviewed against. One tree read of the commit says which
// files exist, so only those are read.
func LoadRules(ctx context.Context, gh RulesReader, repo, base string, paths, contextDocs []string) (*Rules, error) {
	commit, err := gh.BranchHead(ctx, repo, base)
	if err != nil {
		return nil, err
	}
	tree, err := gh.Tree(ctx, repo, commit)
	if err != nil {
		return nil, err
	}
	r := &Rules{Base: base, Commit: commit}
	candidates, unchecked := RuleFiles(paths)
	r.Unchecked = unchecked
	budget := maxRules
	loaded := map[string]bool{}
	reads := 0
	var load func(p string, chain []string) (bool, error)
	load = func(p string, chain []string) (bool, error) {
		switch {
		case slices.Contains(chain, p):
			r.Notes = append(r.Notes, fmt.Sprintf("%s includes itself (through %s); the loop was cut.", p, strings.Join(chain, ", ")))
			return true, nil
		case loaded[p]:
			return true, nil
		case len(chain) > maxIncludeDepth:
			r.Notes = append(r.Notes, fmt.Sprintf("%s is included more than %d levels deep and was not read.", p, maxIncludeDepth))
			return true, nil
		case !tree.Has(p):
			return false, nil
		case reads >= MaxRuleFiles:
			r.Unchecked++
			return true, nil
		}
		reads++
		b, err := gh.FileContent(ctx, repo, p, commit)
		switch {
		case errors.Is(err, github.ErrNotFound):
			return false, nil
		case errors.Is(err, github.ErrTooLarge):
			loaded[p] = true
			r.Files = append(r.Files, RuleFile{Path: p, Omitted: true})
			return true, nil
		case err != nil:
			return false, err
		}
		loaded[p] = true
		incs := includes(string(b))
		if len(incs) > maxIncludesEach {
			r.Notes = append(r.Notes, fmt.Sprintf("%s includes %d files; only the first %d were read.", p, len(incs), maxIncludesEach))
			incs = incs[:maxIncludesEach]
		}
		for _, inc := range incs {
			c, ok := lookupPath(inc)
			if !ok {
				r.Notes = append(r.Notes, fmt.Sprintf("%s includes %q, which is not a repository path.", p, clip(inc, 100)))
				continue
			}
			found, err := load(c, append(slices.Clone(chain), p))
			if err != nil {
				return false, err
			}
			if !found {
				r.Notes = append(r.Notes, fmt.Sprintf("%s includes %s, which %s does not have.", p, c, base))
			}
		}
		r.Files = append(r.Files, bounded(p, b, &budget, maxRuleFile))
		return true, nil
	}
	for _, p := range candidates {
		found, err := load(p, nil)
		if err != nil {
			return nil, err
		}
		if p == RulesFile && !found {
			r.Fallback = true
			if _, err := load(FallbackRules, nil); err != nil {
				return nil, err
			}
		}
	}
	ctxBudget := maxContext
	for _, p := range contextDocs {
		if !tree.Has(p) {
			r.Notes = append(r.Notes, fmt.Sprintf("%s has no %s.", base, p))
			continue
		}
		b, err := gh.FileContent(ctx, repo, p, commit)
		switch {
		case errors.Is(err, github.ErrNotFound):
			r.Notes = append(r.Notes, fmt.Sprintf("%s has no %s.", base, p))
		case errors.Is(err, github.ErrTooLarge):
			r.Context = append(r.Context, RuleFile{Path: p, Omitted: true})
		case err != nil:
			return nil, err
		default:
			r.Context = append(r.Context, bounded(p, b, &ctxBudget, maxContextDoc))
		}
	}
	return r, nil
}

// bounded clips a file to limit bytes and charges it to budget; past the
// budget it is omitted.
func bounded(p string, b []byte, budget *int, limit int) RuleFile {
	f := RuleFile{Path: p, Content: string(b)}
	if len(f.Content) > limit {
		f.Content, f.Clipped = clip(f.Content, limit), true
	}
	if len(f.Content) > *budget {
		f.Content, f.Omitted = "", true
	}
	*budget -= len(f.Content)
	return f
}

// Paths lists the rule files read, in order.
func (r *Rules) Paths() []string {
	out := make([]string, 0, len(r.Files))
	for _, f := range r.Files {
		out = append(out, f.Path)
	}
	return out
}

// changedPaths lists every path a PR touches, with a rename's previous path.
func changedPaths(files []github.PullRequestFile) []string {
	out := make([]string, 0, len(files))
	for _, f := range files {
		out = append(out, f.Path)
		if f.PreviousPath != "" {
			out = append(out, f.PreviousPath)
		}
	}
	return out
}
