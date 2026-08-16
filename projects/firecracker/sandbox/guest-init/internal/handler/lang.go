package handler

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const defaultLanguageFile = "/etc/sandbox-language"

// SearchPath is where every sandbox image installs its toolchain. It is both
// the PATH handed to a snippet and the PATH guest-init must adopt for ITSELF.
//
// exec.Command resolves a bare argv[0] with LookPath against the CALLING
// process's PATH, at construction time, and assigning cmd.Env afterwards does
// not affect that lookup. Firecracker gives PID 1 no environment at all, so
// without AdoptSearchPath the PATH below reaches only a child that is never
// started, and every language fails with:
//
//	exec: "python3": executable file not found in $PATH
const SearchPath = "/usr/bin:/bin:/usr/local/bin"

// EnsureSearchPath puts SearchPath into guest-init's own environment when it
// inherited none, so exec.Command can resolve a bare argv[0]. Call it before
// any exec, including the warm-up, which fails the same way and is
// deliberately non-fatal.
//
// Conditional on purpose. An inherited PATH is deliberate and belongs to
// whoever set it; the fault this repairs is an EMPTY one, which is all a
// Firecracker PID 1 ever has. It also keeps this package's exec-dependent
// tests on their runner's real PATH rather than an image layout that only
// exists inside a guest.
func EnsureSearchPath() error {
	if os.Getenv("PATH") != "" {
		return nil
	}
	return os.Setenv("PATH", SearchPath)
}

// languageFile is indirect so SelectSpec can be tested without writing to /etc.
var languageFile = defaultLanguageFile

// Spec describes how one language prepares, executes, and warms a snippet.
type Spec struct {
	Name           string
	SourceFile     string
	Prepare        func(workdir string) error
	Compile        []string
	Run            []string
	Env            []string
	Warm           []string
	ExcludeOutputs []string
	// CacheDirs are warm caches this language writes, created world-writable on
	// the /tmp tmpfs before warm-up. They must live OUTSIDE any per-invoke
	// workdir (or they come back as output attachments) and they must be data
	// here rather than a name check in cmd/main.go: a language whose cache dir
	// is not listed warms into a directory the request path never reads, which
	// looks identical to a working warm-up in every log line.
	CacheDirs []string
}

var languageSpecs = map[string]Spec{
	"python": {
		Name:       "python",
		SourceFile: "main.py",
		Run:        []string{"python3", "main.py"},
		// Keep matplotlib's font cache outside the request workdir. Otherwise
		// collectOutputFiles returns fontlist.json as a spurious attachment.
		Env: []string{
			"PYTHONUNBUFFERED=1",
			"MPLBACKEND=Agg",
			"MPLCONFIGDIR=" + MPLConfigDir,
			"PYTHONPATH=/opt/sandbox",
		},
		CacheDirs: []string{MPLConfigDir},
		Warm: []string{
			"python3",
			"-c",
			// Rendering a figure, rather than importing alone, builds the font
			// cache before the warm-base snapshot is taken.
			"import matplotlib; matplotlib.use('Agg'); " +
				"import numpy, pandas, scipy, PIL, yaml, dateutil; " +
				"import io, matplotlib.pyplot as plt; " +
				"plt.plot([0, 1], [0, 1]); plt.savefig(io.BytesIO(), format='png')",
		},
		ExcludeOutputs: []string{"main.py"},
	},
	"go": {
		Name:       "go",
		SourceFile: "main.go",
		Prepare:    prepareGo,
		Run:        []string{"go", "run", "."},
		Env: []string{
			// Wolfi ships the go binary built with -trimpath, so it cannot infer
			// its own root and exits 2 with "cannot find GOROOT directory: 'go'
			// binary is trimmed and GOROOT is not set". Verified against the
			// shipped image, which carries a complete root at this path:
			// bin/go, pkg/tool/linux_amd64/compile, and src/runtime.
			"GOROOT=/usr/lib/go",
			"GOCACHE=/tmp/gocache",
			"GOPATH=/tmp/gopath",
			"GOMODCACHE=/tmp/gopath/pkg/mod",
			"GOTOOLCHAIN=local",
			"GOPROXY=off",
			"GOFLAGS=-mod=mod",
			"CGO_ENABLED=0",
			"GOMAXPROCS=1",
		},
		Warm: []string{
			"/bin/sh",
			"-c",
			"mkdir -p /tmp/sandbox-go-warm-src && cd /tmp/sandbox-go-warm-src && printf '%s\\n' " +
				"'package main' 'import (' " +
				"'_ \"bufio\"' '_ \"encoding/json\"' '_ \"fmt\"' '_ \"os\"' " +
				"'_ \"sort\"' '_ \"strconv\"' '_ \"strings\"' '_ \"time\"' " +
				"')' 'func main() {}' > main.go && " +
				"printf '%s\\n' 'module sandbox-warm' '' 'go 1.26' > go.mod && " +
				"go build -o /tmp/sandbox-go-warm .",
		},
		ExcludeOutputs: []string{"main.go", "go.mod", "go.sum"},
		// GOCACHE holds the compiled stdlib the warm-up builds, and it is
		// WRITTEN rather than merely paged in, so it lives on the tmpfs and is
		// charged against the workload's memMib. That is why sandbox-go is
		// sized above the interpreted guests.
		CacheDirs: []string{"/tmp/gocache", "/tmp/gopath"},
	},
	"rust": {
		Name:           "rust",
		SourceFile:     "main.rs",
		Compile:        []string{"rustc", "-O", "main.rs", "-o", "main"},
		Run:            []string{"./main"},
		Warm:           []string{"/bin/sh", "-c", "printf 'fn main() { println!(\"hello\"); }\\n' > /tmp/sandbox-rust-warm.rs && rustc /tmp/sandbox-rust-warm.rs -o /tmp/sandbox-rust-warm"},
		ExcludeOutputs: []string{"main.rs", "main"},
	},
	"elixir": {
		Name:       "elixir",
		SourceFile: "main.exs",
		Run:        []string{"elixir", "main.exs"},
		Env: []string{
			"ERL_CRASH_DUMP=/dev/null",
			// +fnu sets the VM's native name encoding to utf8. Without it every
			// run prints "the VM is running with native name encoding of latin1
			// which may cause Elixir to malfunction as it expects utf8" to
			// stderr, and any snippet touching a non-ASCII filename misbehaves.
			// The usual alternative, a UTF-8 locale, is not available: the guest
			// image ships no locale data.
			"ELIXIR_ERL_OPTIONS=-noinput +fnu",
		},
		Warm:           []string{"elixir", "-e", ":ok"},
		ExcludeOutputs: []string{"main.exs"},
	},
	"ocaml": {
		Name:           "ocaml",
		SourceFile:     "main.ml",
		Run:            []string{"ocaml", "main.ml"},
		Warm:           []string{"/bin/sh", "-c", "printf 'let () = print_endline \"warm\"\\n' > /tmp/sandbox-ocaml-warm.ml && ocaml /tmp/sandbox-ocaml-warm.ml"},
		ExcludeOutputs: []string{"main.ml", "main.cmi", "main.cmo"},
	},
	"javascript": {
		Name:           "javascript",
		SourceFile:     "main.js",
		Run:            []string{"node", "main.js"},
		Warm:           []string{"node", "-e", "0"},
		ExcludeOutputs: []string{"main.js"},
	},
}

// SelectSpec reads the language baked into the image. Existing images without
// the selector file remain Python guests.
func SelectSpec() (Spec, error) {
	data, err := os.ReadFile(languageFile)
	if errors.Is(err, os.ErrNotExist) {
		return languageSpecs["python"], nil
	}
	if err != nil {
		return Spec{}, fmt.Errorf("read sandbox language from %s: %w", languageFile, err)
	}

	name := strings.TrimSpace(string(data))
	spec, ok := languageSpecs[name]
	if !ok {
		return Spec{}, fmt.Errorf("unknown sandbox language %q in %s; valid languages: %s", name, languageFile, strings.Join(validLanguageNames(), ", "))
	}
	return spec, nil
}

// Environment returns the complete explicit environment for a snippet or
// warm-up command. Firecracker gives PID 1 no useful inherited environment.
// HOME and TMPDIR use the writable tmpfs because the rootfs is read-only, and
// request commands point them at the collected, caps-enforced workdir.
func (s Spec) Environment(workdir string) []string {
	env := []string{
		"PATH=" + SearchPath,
		"HOME=" + workdir,
		"TMPDIR=" + workdir,
	}
	return append(env, s.Env...)
}

func validLanguageNames() []string {
	names := make([]string, 0, len(languageSpecs))
	for name := range languageSpecs {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func prepareGo(workdir string) error {
	path := filepath.Join(workdir, "go.mod")
	if _, err := os.Stat(path); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("inspect go.mod: %w", err)
	}
	if err := os.WriteFile(path, []byte("module sandbox\n\ngo 1.26\n"), 0o644); err != nil {
		return fmt.Errorf("write go.mod: %w", err)
	}
	return nil
}
