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

// Bounds on the rules a prompt carries.
const (
	MaxRuleFiles = 100
	maxRuleFile  = 16 << 10
	maxRules     = 64 << 10
)

// RuleFiles lists the REVIEW.md files that may apply to a change: the root
// one and one in every ancestor directory of each changed path (and of a
// rename's previous path). The root comes first, then by depth and name, so
// nearer files come later. Past MaxRuleFiles the deepest are left out and
// counted.
func RuleFiles(paths []string) (files []string, unchecked int) {
	set := map[string]bool{RulesFile: true}
	for _, p := range paths {
		p = cleanPath(p)
		if p == "" {
			continue
		}
		for dir := path.Dir(p); dir != "." && dir != "/"; dir = path.Dir(dir) {
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
	if len(files) > MaxRuleFiles {
		unchecked = len(files) - MaxRuleFiles
		files = files[:MaxRuleFiles]
	}
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
	// Unchecked counts directories past MaxRuleFiles.
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
// rules it is reviewed against.
func LoadRules(ctx context.Context, gh RulesReader, repo, base string, paths, contextDocs []string) (*Rules, error) {
	commit, err := gh.BranchHead(ctx, repo, base)
	if err != nil {
		return nil, err
	}
	r := &Rules{Base: base, Commit: commit}
	candidates, unchecked := RuleFiles(paths)
	r.Unchecked = unchecked
	budget := maxRules
	loaded := map[string]bool{}
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
		}
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
			c := cleanPath(inc)
			if c == "" {
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
