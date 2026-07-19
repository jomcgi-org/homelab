package main

import (
	"reflect"
	"testing"
)

// TestValidateExpr is the defense-in-depth gate on the visitor-supplied cquery
// expression (ADR embervm/010 Security; the ember_public edge validates first,
// this is the guest's independent second gate). It must accept the ordinary
// Abseil query shapes the demo advertises and reject anything that could smuggle
// a flag (a leading `-` token becomes a bazel option since expr is not
// shell-quoted away, but IS a distinct argv element), inject a second command,
// or blow the length/line budget.
func TestValidateExpr(t *testing.T) {
	for _, tc := range []struct {
		name    string
		expr    string
		wantErr bool
	}{
		// Accepted: the advertised example queries.
		{"deps", "deps(//absl/strings)", false},
		{"kind with quoted class", `kind("cc_library", //absl/...)`, false},
		{"somepath two args", "somepath(//absl/base, //absl/time)", false},
		{"target pattern wildcard", "//absl/...", false},
		{"single label", "//absl/strings:strings", false},
		{"rdeps", "rdeps(//absl/..., //absl/base)", false},
		{"attr with regex-ish chars", `attr(name, ".*test.*", //absl/...)`, false},

		// Rejected: flag smuggling. `--output=starlark` is code execution (ADR
		// Security); any whitespace-delimited token starting with `-` is refused
		// even though the charset allows `-` inside a token (e.g. a target name).
		{"leading dash flag", "--output=starlark", true},
		{"flag after expr", "//absl/... --keep_going", true},
		{"short flag token", "deps(//absl/base) -k", true},

		// Rejected: multi-line (a newline could carry a second directive past a
		// naive edge check).
		{"newline", "deps(//absl/base)\ndeps(//absl/time)", true},
		{"carriage return", "deps(//absl/base)\rrm -rf", true},

		// Rejected: empty / whitespace-only.
		{"empty", "", true},
		{"whitespace only", "   ", true},

		// Rejected: charset. Backticks, semicolons, pipes, dollars, braces are
		// not query syntax and are classic shell/command injection carriers even
		// though expr is passed as one argv element (belt and braces).
		{"semicolon", "deps(//absl/base); ls", true},
		{"backtick", "deps(`whoami`)", true},
		{"pipe", "deps(//absl/base) | cat", true},
		{"dollar", "deps($FOO)", true},
		{"brace", "deps({//absl/base})", true},

		// Rejected: over the 512-char length cap.
		{"too long", "deps(" + longLabel(600) + ")", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateExpr(tc.expr)
			if tc.wantErr && err == nil {
				t.Fatalf("validateExpr(%q) = nil, want error", tc.expr)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("validateExpr(%q) = %v, want nil", tc.expr, err)
			}
		})
	}
}

// longLabel returns a filler string of n non-special characters for the length
// cap test.
func longLabel(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = 'a'
	}
	return string(b)
}

// TestBuildArgvGolden freezes the exact argv buildArgv emits (ADR embervm/010
// condition 2: buildArgv is the SINGLE source of truth for warming AND serving,
// and any silent flag drift discards the analysis cache, turning a "0 packages
// loaded" warm restore back into a cold re-analysis). A golden slice makes any
// edit to the flag set loud in review, and asserts the expression lands as
// EXACTLY one argv element (never shell-split, never concatenated into a flag).
func TestBuildArgvGolden(t *testing.T) {
	got := buildArgv("deps(//absl/strings)")
	want := []string{
		"/usr/local/bin/bazel",
		"--output_user_root=/tmp/bazel",
		"--host_jvm_args=-Xmx1g",
		"--max_idle_secs=0",
		"cquery",
		"deps(//absl/strings)",
		"--noenable_bzlmod",
		"--distdir=/opt/distdir",
		"--experimental_convenience_symlinks=ignore",
		"--output=label",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("buildArgv drift:\n got = %#v\nwant = %#v", got, want)
	}
}

// TestBuildArgvExprIsOneElement guards the injection-resistance property
// directly: whatever the expression contains (spaces, quotes, parens), it must
// occupy a single argv slot, so bazel receives it as one opaque query string and
// never as extra flags or commands.
func TestBuildArgvExprIsOneElement(t *testing.T) {
	expr := `kind("cc_library", //absl/...)`
	argv := buildArgv(expr)
	found := 0
	idx := -1
	for i, a := range argv {
		if a == expr {
			found++
			idx = i
		}
	}
	if found != 1 {
		t.Fatalf("expr appeared %d times in argv, want exactly 1: %#v", found, argv)
	}
	// It must sit immediately after the `cquery` command token.
	if idx == 0 || argv[idx-1] != "cquery" {
		t.Fatalf("expr at index %d not directly after cquery: %#v", idx, argv)
	}
}

// TestAnalyzedLineFromStderr covers the proof-of-restore extractor: it pulls the
// `Analyzed ...` progress line bazel writes to stderr, which carries the
// "(0 packages loaded, 0 targets configured)" phrase that PROVES a restored
// clone reused the snapshot's Skyframe graph rather than re-loading. A missing
// line yields "" (the caller logs a drift warning rather than failing).
func TestAnalyzedLineFromStderr(t *testing.T) {
	for _, tc := range []struct {
		name   string
		stderr string
		want   string
	}{
		{
			"warm restore proof line",
			"Starting local Bazel server\nAnalyzed 514 targets (0 packages loaded, 0 targets configured).\nLoading: 0 packages loaded",
			"Analyzed 514 targets (0 packages loaded, 0 targets configured).",
		},
		{
			"cold analysis line",
			"INFO: Analyzed 514 targets (77 packages loaded, 2293 targets configured).\n",
			"Analyzed 514 targets (77 packages loaded, 2293 targets configured).",
		},
		{"no analyzed line", "ERROR: no such package '//nope'\n", ""},
		{"empty", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := analyzedLineFromStderr(tc.stderr); got != tc.want {
				t.Fatalf("analyzedLineFromStderr() = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestTruncate covers the stdout label-list cap: the labels payload is bounded at
// maxOutput bytes so a wildcard query cannot return an unbounded body over the
// vsock transport, with the boolean flag telling the caller (and the visitor)
// that the list was cut.
func TestTruncate(t *testing.T) {
	short := "//absl/strings\n//absl/base\n"
	if s, trunc := truncate([]byte(short), 1024); trunc || s != short {
		t.Fatalf("truncate(short) = (%q, %v), want (unchanged, false)", s, trunc)
	}
	big := make([]byte, maxOutput+100)
	for i := range big {
		big[i] = 'x'
	}
	s, trunc := truncate(big, maxOutput)
	if !trunc {
		t.Fatalf("truncate(big) truncated = false, want true")
	}
	if len(s) != maxOutput {
		t.Fatalf("truncate(big) len = %d, want %d", len(s), maxOutput)
	}
}
