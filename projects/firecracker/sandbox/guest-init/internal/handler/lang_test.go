package handler

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// TestRunCommandResolvesBareArgvWithNoInheritedPath drives the real exec path
// with the environment a Firecracker guest actually has: none.
//
// This crosses the seam the Environment() assertions stop short of. A correct
// PATH in the returned slice proves nothing on its own, because exec.Command
// resolves argv[0] against the CALLING process's PATH and never looks at
// cmd.Env. That gap failed all six languages in production with
// `exec: "python3": executable file not found in $PATH` while every
// env-slice assertion in this file stayed green.
func TestRunCommandResolvesBareArgvWithNoInheritedPath(t *testing.T) {
	t.Setenv("PATH", "")
	if _, err := exec.LookPath("sh"); err == nil {
		t.Fatal("precondition failed: a bare name resolved with an empty PATH")
	}

	stdout := &capBuffer{limit: 4096}
	stderr := &capBuffer{limit: 4096}
	err := runCommand(context.Background(), Spec{Name: "test"}, t.TempDir(),
		[]string{"sh", "-c", "echo resolved"}, stdout, stderr)
	if err != nil {
		t.Fatalf("runCommand with a bare argv[0] and no inherited PATH: %v (stderr %q)", err, stderr.buf.String())
	}
	if got := strings.TrimSpace(stdout.buf.String()); got != "resolved" {
		t.Errorf("stdout = %q, want %q", got, "resolved")
	}
}

// TestEnsureSearchPathLeavesAnInheritedPathAlone pins the conditional. An
// inherited PATH belongs to whoever set it, and clobbering it would point this
// package's exec-dependent tests at an image layout that exists only in a guest.
func TestEnsureSearchPathLeavesAnInheritedPathAlone(t *testing.T) {
	t.Setenv("PATH", "/deliberate/path")
	if err := EnsureSearchPath(); err != nil {
		t.Fatalf("EnsureSearchPath: %v", err)
	}
	if got := os.Getenv("PATH"); got != "/deliberate/path" {
		t.Errorf("inherited PATH was overwritten: got %q", got)
	}
}

func TestSelectSpec(t *testing.T) {
	originalPath := languageFile
	t.Cleanup(func() { languageFile = originalPath })

	selector := filepath.Join(t.TempDir(), "sandbox-language")
	languageFile = selector
	for _, name := range validLanguageNames() {
		t.Run(name, func(t *testing.T) {
			if err := os.WriteFile(selector, []byte(" \n"+name+"\t\n"), 0o644); err != nil {
				t.Fatalf("write selector: %v", err)
			}
			spec, err := SelectSpec()
			if err != nil {
				t.Fatalf("SelectSpec: %v", err)
			}
			if spec.Name != name {
				t.Errorf("Name = %q, want %q", spec.Name, name)
			}
		})
	}
}

func TestSelectSpecMissingDefaultsToPython(t *testing.T) {
	originalPath := languageFile
	languageFile = filepath.Join(t.TempDir(), "missing")
	t.Cleanup(func() { languageFile = originalPath })

	spec, err := SelectSpec()
	if err != nil {
		t.Fatalf("SelectSpec: %v", err)
	}
	if spec.Name != "python" {
		t.Errorf("Name = %q, want python", spec.Name)
	}
}

func TestSelectSpecRejectsUnknownLanguage(t *testing.T) {
	originalPath := languageFile
	selector := filepath.Join(t.TempDir(), "sandbox-language")
	languageFile = selector
	t.Cleanup(func() { languageFile = originalPath })
	if err := os.WriteFile(selector, []byte("fortran\n"), 0o644); err != nil {
		t.Fatalf("write selector: %v", err)
	}

	_, err := SelectSpec()
	if err == nil {
		t.Fatal("SelectSpec returned nil error for an unknown language")
	}
	for _, want := range append([]string{"fortran"}, validLanguageNames()...) {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not list %q", err, want)
		}
	}
}

func TestLanguageSpecs(t *testing.T) {
	tests := []struct {
		name       string
		sourceFile string
		compile    []string
		run        []string
		extraEnv   []string
		warm       []string
		excluded   []string
	}{
		{name: "python", sourceFile: "main.py", run: []string{"python3", "main.py"}, extraEnv: []string{"PYTHONUNBUFFERED=1", "MPLBACKEND=Agg", "MPLCONFIGDIR=/tmp/mplconfig", "PYTHONPATH=/opt/sandbox"}, warm: []string{"python3", "-c"}, excluded: []string{"main.py"}},
		{name: "go", sourceFile: "main.go", run: []string{"go", "run", "."}, extraEnv: []string{"GOROOT=/usr/lib/go", "GOCACHE=/tmp/gocache", "GOPATH=/tmp/gopath", "GOMODCACHE=/tmp/gopath/pkg/mod", "GOTOOLCHAIN=local", "GOPROXY=off", "GOFLAGS=-mod=mod", "CGO_ENABLED=0", "GOMAXPROCS=1", "TMPDIR=/tmp"}, warm: []string{"/bin/sh", "-c"}, excluded: []string{"main.go", "go.mod", "go.sum"}},
		{name: "rust", sourceFile: "main.rs", compile: []string{"rustc", "-O", "main.rs", "-o", "main"}, run: []string{"./main"}, warm: []string{"/bin/sh", "-c"}, excluded: []string{"main.rs", "main"}},
		{name: "elixir", sourceFile: "main.exs", run: []string{"elixir", "main.exs"}, extraEnv: []string{"ERL_CRASH_DUMP=/dev/null", "ELIXIR_ERL_OPTIONS=-noinput +fnu"}, warm: []string{"elixir", "-e", ":ok"}, excluded: []string{"main.exs"}},
		{name: "ocaml", sourceFile: "main.ml", run: []string{"ocaml", "main.ml"}, warm: []string{"/bin/sh", "-c"}, excluded: []string{"main.ml", "main.cmi", "main.cmo"}},
		{name: "javascript", sourceFile: "main.js", run: []string{"node", "main.js"}, warm: []string{"node", "-e", "0"}, excluded: []string{"main.js"}},
	}
	// Only genuinely language-neutral variables belong here. PYTHONUNBUFFERED is
	// deliberately NOT one: it is python's, and lives in that spec's Env.
	commonEnv := []string{
		"PATH=/usr/bin:/bin:/usr/local/bin",
		"HOME=/tmp/workdir",
		"TMPDIR=/tmp/workdir",
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			spec := languageSpecs[tt.name]
			if spec.Name != tt.name {
				t.Errorf("Name = %q, want %q", spec.Name, tt.name)
			}
			if spec.SourceFile != tt.sourceFile {
				t.Errorf("SourceFile = %q, want %q", spec.SourceFile, tt.sourceFile)
			}
			if !reflect.DeepEqual(spec.Compile, tt.compile) {
				t.Errorf("Compile = %#v, want %#v", spec.Compile, tt.compile)
			}
			if !reflect.DeepEqual(spec.Run, tt.run) {
				t.Errorf("Run = %#v, want %#v", spec.Run, tt.run)
			}
			wantEnv := append(append([]string{}, commonEnv...), tt.extraEnv...)
			if got := spec.Environment("/tmp/workdir"); !reflect.DeepEqual(got, wantEnv) {
				t.Errorf("Environment = %#v, want %#v", got, wantEnv)
			}
			if !reflect.DeepEqual(spec.ExcludeOutputs, tt.excluded) {
				t.Errorf("ExcludeOutputs = %#v, want %#v", spec.ExcludeOutputs, tt.excluded)
			}
			if !contains(spec.ExcludeOutputs, spec.SourceFile) {
				t.Errorf("ExcludeOutputs %#v does not contain source %q", spec.ExcludeOutputs, spec.SourceFile)
			}
			if len(spec.Warm) < len(tt.warm) || !reflect.DeepEqual(spec.Warm[:len(tt.warm)], tt.warm) {
				t.Errorf("Warm = %#v, want prefix %#v", spec.Warm, tt.warm)
			}
			if (spec.Prepare != nil) != (tt.name == "go") {
				t.Errorf("Prepare presence = %t, want %t", spec.Prepare != nil, tt.name == "go")
			}
		})
	}
}

func TestGoEnvironmentIsOffline(t *testing.T) {
	env := languageSpecs["go"].Environment("/tmp/workdir")
	for _, required := range []string{"GOTOOLCHAIN=local", "GOPROXY=off"} {
		if !contains(env, required) {
			t.Errorf("Go environment %#v does not contain %q", env, required)
		}
	}
}

// TestGoEnvironmentSetsGoroot pins the variable without which the go binary
// exits 2 before compiling anything, because Wolfi trims it and it cannot then
// infer its own root.
//
// This is a data assertion and it is worth being honest about its limits: it
// proves the string is in the slice, not that /usr/lib/go is where this image
// actually puts the toolchain. That was established by reading the shipped
// image (bin/go, pkg/tool/linux_amd64/compile, src/runtime/proc.go) and is
// only ever re-proved by running go in the sandbox.
func TestGoEnvironmentSetsGoroot(t *testing.T) {
	env := languageSpecs["go"].Environment("/tmp/workdir")
	if !contains(env, "GOROOT=/usr/lib/go") {
		t.Errorf("Go environment %#v does not contain GOROOT", env)
	}
}

// TestGoTmpdirOverridesTheWorkdir pins the override that lets go read the
// go.mod prepareGo writes. Go ignores a go.mod sitting in os.TempDir() itself,
// and the base environment points TMPDIR at the workdir, which is that same
// directory.
//
// The assertion is deliberately about ORDER, not membership. os/exec keeps the
// last duplicate, so a TMPDIR=/tmp placed before the base entry would satisfy a
// contains() check while leaving go reading go.mod out of the temp root and
// failing exactly as it does in production.
func TestGoTmpdirOverridesTheWorkdir(t *testing.T) {
	env := languageSpecs["go"].Environment("/tmp/workdir")
	effective := ""
	for _, kv := range env {
		if strings.HasPrefix(kv, "TMPDIR=") {
			effective = kv
		}
	}
	if effective != "TMPDIR=/tmp" {
		t.Errorf("effective TMPDIR = %q, want %q", effective, "TMPDIR=/tmp")
	}
}

// TestElixirRunsWithUtf8NameEncoding pins +fnu. The image ships no locale data,
// so without it the BEAM starts in latin1 and warns on every run.
func TestElixirRunsWithUtf8NameEncoding(t *testing.T) {
	env := languageSpecs["elixir"].Environment("/tmp/workdir")
	if !contains(env, "ELIXIR_ERL_OPTIONS=-noinput +fnu") {
		t.Errorf("Elixir environment %#v does not request utf8 name encoding", env)
	}
}

func TestWarmCommandsCoverRequiredRuntimeWork(t *testing.T) {
	pythonWarm := []string{
		"python3",
		"-c",
		"import matplotlib; matplotlib.use('Agg'); " +
			"import numpy, pandas, scipy, PIL, yaml, dateutil; " +
			"import io, matplotlib.pyplot as plt; " +
			"plt.plot([0, 1], [0, 1]); plt.savefig(io.BytesIO(), format='png')",
	}
	if got := languageSpecs["python"].Warm; !reflect.DeepEqual(got, pythonWarm) {
		t.Errorf("Python Warm = %#v, want exact existing warm import command %#v", got, pythonWarm)
	}

	goWarm := strings.Join(languageSpecs["go"].Warm, " ")
	for _, required := range []string{"cd /tmp", "go build", "fmt", "os", "strings", "strconv", "sort", "time", "encoding/json", "bufio"} {
		if !strings.Contains(goWarm, required) {
			t.Errorf("Go Warm %q does not contain %q", goWarm, required)
		}
	}

	for _, tt := range []struct {
		name     string
		required []string
	}{
		{name: "rust", required: []string{"rustc", "/tmp/", "hello"}},
		{name: "ocaml", required: []string{"ocaml", "/tmp/", ".ml"}},
	} {
		warm := strings.Join(languageSpecs[tt.name].Warm, " ")
		for _, required := range tt.required {
			if !strings.Contains(warm, required) {
				t.Errorf("%s Warm %q does not contain %q", tt.name, warm, required)
			}
		}
	}
}

func TestPrepareGoCreatesModuleOnlyWhenMissing(t *testing.T) {
	dir := t.TempDir()
	if err := prepareGo(dir); err != nil {
		t.Fatalf("prepareGo missing module: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil {
		t.Fatalf("read generated go.mod: %v", err)
	}
	if got, want := string(data), "module sandbox\n\ngo 1.26\n"; got != want {
		t.Errorf("generated go.mod = %q, want %q", got, want)
	}

	const supplied = "module caller.example/custom\n\ngo 1.26\n"
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte(supplied), 0o644); err != nil {
		t.Fatalf("write caller go.mod: %v", err)
	}
	if err := prepareGo(dir); err != nil {
		t.Fatalf("prepareGo supplied module: %v", err)
	}
	data, err = os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil {
		t.Fatalf("read caller go.mod: %v", err)
	}
	if string(data) != supplied {
		t.Errorf("prepareGo replaced caller go.mod with %q", data)
	}
}

func TestRustSpecExcludesCompiledBinary(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "main.rs", []byte("fn main() {}"))
	writeFile(t, dir, "main", []byte("compiled binary"))
	writeFile(t, dir, "result.txt", []byte("result"))

	files, truncated, err := collectOutputFiles(dir, nil, languageSpecs["rust"].ExcludeOutputs)
	if err != nil {
		t.Fatalf("collectOutputFiles: %v", err)
	}
	if truncated {
		t.Error("truncated = true, want false")
	}
	got := make(map[string]bool)
	for _, file := range files {
		got[file.Path] = true
	}
	if got["main.rs"] || got["main"] {
		t.Errorf("rust build artifacts returned in files: %#v", got)
	}
	if !got["result.txt"] {
		t.Errorf("result.txt missing from files: %#v", got)
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

// TestCacheDirsCoverTmpEnvPaths guards the inert-warm-up trap: warm-up runs
// with Spec.Environment, so a cache the language is pointed at via env must
// also be created (world-writable, on the tmpfs) via CacheDirs. If it is not,
// warm-up still exits 0 and still logs "language warm-up done" while writing
// into a directory the request path never reads, so the guest ships cold with
// no failing signal anywhere.
func TestCacheDirsCoverTmpEnvPaths(t *testing.T) {
	for name, spec := range languageSpecs {
		for _, kv := range spec.Env {
			_, value, ok := strings.Cut(kv, "=")
			if !ok || !strings.HasPrefix(value, "/tmp/") {
				continue
			}
			covered := false
			for _, dir := range spec.CacheDirs {
				if value == dir || strings.HasPrefix(value, dir+"/") {
					covered = true
					break
				}
			}
			if !covered {
				t.Errorf("%s: env %q points at a tmpfs path no CacheDirs entry creates; warm-up would populate an unread directory", name, kv)
			}
		}
	}
}
