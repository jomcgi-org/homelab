package cliout

import (
	"path/filepath"
	"testing"
)

const sampleCliOutput = `{"results":[{"check_id":"python.lang.security.audit.hardcoded-password","path":"app/config.py","start":{"line":12,"col":5},"extra":{"message":"Hardcoded password detected","severity":"ERROR"}}],"errors":[{"message":"a rule failed to parse"},{"message":""}]}
`

func TestParse_Flattens(t *testing.T) {
	res, err := Parse([]byte(sampleCliOutput), "")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	if len(res.Findings) != 1 {
		t.Fatalf("want 1 finding, got %d", len(res.Findings))
	}
	got := res.Findings[0]
	if got.Path != "app/config.py" || got.Line != 12 || got.Col != 5 ||
		got.RuleID != "python.lang.security.audit.hardcoded-password" ||
		got.Severity != "ERROR" || got.Message != "Hardcoded password detected" {
		t.Fatalf("finding mismatch: %+v", got)
	}

	if len(res.Errors) != 1 || res.Errors[0] != "a rule failed to parse" {
		t.Fatalf("errors = %v, want [a rule failed to parse] (empty message dropped)", res.Errors)
	}

	if string(res.RawCliOutput) != sampleCliOutput {
		t.Fatalf("RawCliOutput mismatch:\n got  %s\n want %s", res.RawCliOutput, sampleCliOutput)
	}
}

func TestParse_StripsPrefix(t *testing.T) {
	prefix := filepath.FromSlash("/tmp/sgfull-abc")
	absPath := filepath.Join(prefix, "pkg/b.py")
	line := []byte(`{"results":[{"check_id":"rule.id","path":"` + filepath.ToSlash(absPath) + `","start":{"line":1,"col":1},"extra":{"message":"m","severity":"WARNING"}}],"errors":[]}
`)

	res, err := Parse(line, prefix)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(res.Findings) != 1 {
		t.Fatalf("want 1 finding, got %d", len(res.Findings))
	}
	if got, want := res.Findings[0].Path, "pkg/b.py"; got != want {
		t.Fatalf("Path = %q, want %q", got, want)
	}

	// RawCliOutput must still contain the original absolute path bytes
	// verbatim, not the rewritten repo-relative one.
	if string(res.RawCliOutput) != string(line) {
		t.Fatalf("RawCliOutput mismatch:\n got  %s\n want %s", res.RawCliOutput, line)
	}
}

func TestParse_NoPrefixLeavesPathsUnchanged(t *testing.T) {
	res, err := Parse([]byte(sampleCliOutput), "")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(res.Findings) != 1 || res.Findings[0].Path != "app/config.py" {
		t.Fatalf("Path should be unchanged with empty stripPrefix, got %+v", res.Findings)
	}
}

func TestParse_InvalidJSON(t *testing.T) {
	if _, err := Parse([]byte("not json"), ""); err == nil {
		t.Fatal("want error for invalid cli_output, got nil")
	}
}
