package main

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestParseMeminfoFields(t *testing.T) {
	for _, tc := range []struct {
		name    string
		input   string
		want    map[string]uint64
		wantErr bool
	}{
		{
			"success",
			"MemTotal:       3072000 kB\nMemFree:         245760 kB\nMemAvailable:     319488 kB\nCached:            68608 kB\n",
			map[string]uint64{"MemTotal": 3000, "MemFree": 240, "MemAvailable": 312, "Cached": 67},
			false,
		},
		{"missing field", "MemTotal: 1024 kB\nMemFree: 512 kB\nCached: 256 kB\n", nil, true},
		{"malformed value", "MemTotal: nope kB\nMemFree: 512 kB\nMemAvailable: 512 kB\nCached: 256 kB\n", nil, true},
		{"malformed unit", "MemTotal: 1024 MB\nMemFree: 512 kB\nMemAvailable: 512 kB\nCached: 256 kB\n", nil, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseMeminfoFields(tc.input)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseMeminfoFields() error = nil, want error")
				}
				return
			}
			if err != nil {
				t.Fatalf("parseMeminfoFields() error = %v, want nil", err)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("parseMeminfoFields() = %#v, want %#v", got, tc.want)
			}
		})
	}
}

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
		"--host_jvm_args=-Djava.security.egd=file:/dev/urandom",
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

// TestQueryErrorBody covers the failure-payload shape. A bad visitor query (a
// bazel non-zero exit, an in-guest timeout, or a validation rejection) is a
// SUCCESSFUL demo run whose payload carries the failure, so handleQuery returns
// HTTP 200 with this body and NO labels/analyzed_line. EmberVM's task pipeline
// only relays a successful-task guest response verbatim; a guest non-2xx would be
// dead-lettered and the visitor would never see bazel's error. So the body must
// carry the error text, the exit code, and wall_ms, and must NOT carry a labels
// or analyzed_line key (their presence is the success discriminator on the edge).
func TestQueryErrorBody(t *testing.T) {
	raw := queryErrorBody("ERROR: no such target '//absl/stringsm'", 1, 240)

	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("queryErrorBody produced invalid JSON: %v", err)
	}
	if got["error"] != "ERROR: no such target '//absl/stringsm'" {
		t.Fatalf("error = %v, want the stderr text", got["error"])
	}
	// JSON numbers decode to float64 through any.
	if got["exit_code"].(float64) != 1 {
		t.Fatalf("exit_code = %v, want 1", got["exit_code"])
	}
	if got["wall_ms"].(float64) != 240 {
		t.Fatalf("wall_ms = %v, want 240", got["wall_ms"])
	}
	if _, ok := got["labels"]; ok {
		t.Fatalf("error body must NOT carry a labels key: %v", got)
	}
	if _, ok := got["analyzed_line"]; ok {
		t.Fatalf("error body must NOT carry an analyzed_line key: %v", got)
	}
}

// TestQuerySuccessBodyHasNoError guards the success discriminator from the other
// side: a successful query's body carries labels + analyzed_line and NO error
// key, so the edge (bazel_core.run_query) can branch on error-presence alone.
func TestQuerySuccessBodyHasNoError(t *testing.T) {
	raw, err := json.Marshal(queryResult{
		Labels:       "//absl/strings:strings\n",
		Truncated:    false,
		AnalyzedLine: "Analyzed 3 targets (0 packages loaded, 0 targets configured).",
		WallMs:       120,
	})
	if err != nil {
		t.Fatalf("marshal queryResult: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if _, ok := got["error"]; ok {
		t.Fatalf("success body must NOT carry an error key: %v", got)
	}
	if got["labels"] == "" || got["analyzed_line"] == "" {
		t.Fatalf("success body must carry labels + analyzed_line: %v", got)
	}
}
