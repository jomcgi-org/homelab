package buildbuddy

import (
	"context"
	"go/ast"
	goparser "go/parser"
	"go/token"
	"path"
	"regexp"
	"sort"
	"strings"

	"github.com/bazelbuild/buildtools/build"
)

// MaxSourceFiles bounds base-tree verification, independently of log paging.
const MaxSourceFiles = 128

var (
	targetID   = regexp.MustCompile(`^` + label + `$`)
	pythonID   = regexp.MustCompile(`^([A-Za-z0-9_\-./]+\.py)::([A-Za-z_][A-Za-z0-9_]*)(?:::([A-Za-z_][A-Za-z0-9_]*))?$`)
	goID       = regexp.MustCompile(`^Test[A-Za-z0-9_]+$`)
	jsID       = regexp.MustCompile(`^[A-Za-z0-9_\-./]+\.(?:test|spec)\.[cm]?[jt]sx?$`)
	pythonDecl = regexp.MustCompile(`^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:(]`)
)

// Filter is the trust boundary between CI candidates and names allowed in a
// revision. files and read MUST describe the same immutable base commit on
// main, never the PR head, a merge group or a working directory. A missing
// definition is dropped. Unsupported/dynamic definitions fail closed.
func Filter(ctx context.Context, f *Failures, files map[string]bool, read func(context.Context, string) ([]byte, error)) (*Failures, error) {
	out := &Failures{Truncated: f.Truncated}
	cache := map[string][]byte{}
	total := 0
	get := func(name string) ([]byte, error) {
		if !files[name] || !cleanPath(name) {
			return nil, nil
		}
		if data, ok := cache[name]; ok {
			return data, nil
		}
		if len(cache) >= MaxSourceFiles || total >= 8<<20 {
			out.Truncated = true
			return nil, nil
		}
		data, err := read(ctx, name)
		if err != nil {
			return nil, err
		}
		if len(data) > 1<<20 || total+len(data) > 8<<20 {
			out.Truncated = true
			data = nil
		}
		cache[name] = data
		total += len(data)
		return data, nil
	}
	packages := map[string]bool{}
	for _, id := range f.Targets {
		if len(out.Targets) >= MaxTargets {
			out.Truncated = true
			break
		}
		if len(id) > maxIDLength || !targetID.MatchString(id) {
			continue
		}
		pkg, name, _ := strings.Cut(strings.TrimPrefix(id, "//"), ":")
		if (pkg != "" && !cleanPath(pkg)) || !cleanPath(name) {
			continue
		}
		file := path.Join(pkg, "BUILD.bazel")
		if !files[file] {
			file = path.Join(pkg, "BUILD")
		}
		data, err := get(file)
		if err != nil {
			return nil, err
		}
		if definesTarget(file, data, name) {
			out.Targets = append(out.Targets, id)
			packages[path.Dir(file)] = true
		}
	}
	// Go's log has no path. Search test files in the verified failing packages.
	// Without a target, search the base tree within the same file/byte bound.
	goNames := map[string]bool{}
	needGo := false
	for _, id := range f.Tests {
		needGo = needGo || goID.MatchString(id)
	}
	if needGo {
		var paths []string
		for file := range files {
			if strings.HasSuffix(file, "_test.go") && (len(packages) == 0 || packages[path.Dir(file)]) {
				paths = append(paths, file)
			}
		}
		sort.Strings(paths)
		for _, file := range paths {
			data, err := get(file)
			if err != nil {
				return nil, err
			}
			if len(data) == 0 {
				continue
			}
			parsed, err := goparser.ParseFile(token.NewFileSet(), file, data, 0)
			if err != nil {
				continue
			}
			for _, decl := range parsed.Decls {
				fn, ok := decl.(*ast.FuncDecl)
				if ok && fn.Recv == nil && fn.Body != nil && goID.MatchString(fn.Name.Name) &&
					fn.Type.Params.NumFields() == 1 && fn.Type.Results.NumFields() == 0 {
					param, pointer := fn.Type.Params.List[0].Type.(*ast.StarExpr)
					if !pointer {
						continue
					}
					typ, qualified := param.X.(*ast.SelectorExpr)
					if !qualified || typ.Sel.Name != "T" {
						continue
					}
					goNames[fn.Name.Name] = true
				}
			}
		}
	}
	for _, id := range f.Tests {
		if len(out.Tests) >= MaxTests {
			out.Truncated = true
			break
		}
		if len(id) > maxIDLength {
			continue
		}
		ok := goNames[id]
		if m := pythonID.FindStringSubmatch(id); m != nil && pythonTestFile(m[1]) {
			data, err := get(m[1])
			if err != nil {
				return nil, err
			}
			ok = definesPythonTest(string(data), m[2], m[3])
		} else if jsID.MatchString(id) && cleanPath(id) {
			ok = files[id]
		}
		if ok {
			out.Tests = append(out.Tests, id)
		}
	}
	return out, nil
}

func cleanPath(p string) bool {
	return p != "" && p != "." && !strings.HasPrefix(p, "/") && path.Clean(p) == p && !strings.HasPrefix(p, "../") && p != ".."
}

func definesTarget(file string, data []byte, name string) bool {
	if len(data) == 0 {
		return false
	}
	f, err := build.ParseBuild(file, data)
	if err != nil {
		return false
	}
	for _, stmt := range f.Stmt {
		call, ok := stmt.(*build.CallExpr)
		if !ok {
			continue
		}
		for _, arg := range call.List {
			attr, ok := arg.(*build.AssignExpr)
			if !ok || attr.Op != "=" {
				continue
			}
			key, ok := attr.LHS.(*build.Ident)
			value, literal := attr.RHS.(*build.StringExpr)
			if ok && literal && key.Name == "name" && value.Value == name {
				return true
			}
		}
	}
	return false
}

func pythonTestFile(file string) bool {
	base := path.Base(file)
	return cleanPath(file) && (strings.HasPrefix(base, "test_") || strings.HasSuffix(base, "_test.py"))
}

func definesPythonTest(source, first, method string) bool {
	// Remove strings and comments before looking for declarations, so examples
	// in docstrings cannot authorize a CI-supplied name. Preserve indentation.
	source = pythonCode(source)
	class, classIndent := "", -1
	for _, line := range strings.Split(source, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " \t"))
		if indent <= classIndent {
			class, classIndent = "", -1
		}
		m := pythonDecl.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		if m[2] == "class" && indent == 0 {
			class, classIndent = m[3], indent
		}
		if m[2] != "def" {
			continue
		}
		if method == "" && indent == 0 && strings.HasPrefix(first, "test_") && m[3] == first {
			return true
		}
		if method != "" && class == first && strings.HasPrefix(first, "Test") && strings.HasPrefix(method, "test_") && m[3] == method {
			return true
		}
	}
	return false
}

func pythonCode(s string) string {
	out := []byte(s)
	for i := 0; i < len(s); {
		start := i
		switch s[i] {
		case '#':
			for i < len(s) && s[i] != '\n' {
				i++
			}
		case '\'', '"':
			quote := s[i : i+1]
			if strings.HasPrefix(s[i:], strings.Repeat(quote, 3)) {
				quote = strings.Repeat(quote, 3)
			}
			i += len(quote)
			for i < len(s) {
				if s[i] == '\\' {
					i += 2
					continue
				}
				if strings.HasPrefix(s[i:], quote) {
					i += len(quote)
					break
				}
				i++
			}
		default:
			i++
			continue
		}
		for j := start; j < i && j < len(out); j++ {
			if out[j] != '\n' && out[j] != '\r' {
				out[j] = ' '
			}
		}
	}
	return string(out)
}
