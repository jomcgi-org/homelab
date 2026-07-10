package scandriver

import (
	"encoding/json"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// representative semgrep --json cli_output with one result carrying the
// fingerprint-bearing fields (extra.metadata, extra.fingerprint) that the
// flattened vsockproto.Finding drops.
const sampleCliOutput = `{"results":[{"check_id":"python.lang.security.audit.hardcoded-password","path":"app/config.py","start":{"line":12,"col":5},"end":{"line":12,"col":30},"extra":{"message":"Hardcoded password detected","severity":"ERROR","fingerprint":"abc123def456","metadata":{"cwe":["CWE-798"],"owasp":["A07:2021"]},"metavars":{"$PW":{"start":{"line":12,"col":5},"end":{"line":12,"col":10}}}}}],"errors":[]}
`

func TestParseCliOutput_FlattensFindings(t *testing.T) {
	res, err := parseCliOutput([]byte(sampleCliOutput))
	if err != nil {
		t.Fatalf("parseCliOutput: %v", err)
	}

	if len(res.Findings) != 1 {
		t.Fatalf("want 1 finding, got %d", len(res.Findings))
	}
	want := vsockproto.Finding{
		Path:     "app/config.py",
		Line:     12,
		Col:      5,
		RuleID:   "python.lang.security.audit.hardcoded-password",
		Severity: "ERROR",
		Message:  "Hardcoded password detected",
	}
	if got := res.Findings[0]; got != want {
		t.Fatalf("finding mismatch:\n got  %+v\n want %+v", got, want)
	}
	if len(res.Errors) != 0 {
		t.Fatalf("want no errors, got %v", res.Errors)
	}
}

func TestParseCliOutput_PreservesRawCliOutput(t *testing.T) {
	res, err := parseCliOutput([]byte(sampleCliOutput))
	if err != nil {
		t.Fatalf("parseCliOutput: %v", err)
	}

	if string(res.RawCliOutput) != sampleCliOutput {
		t.Fatalf("RawCliOutput mismatch:\n got  %s\n want %s", res.RawCliOutput, sampleCliOutput)
	}

	// The fingerprint-bearing fields that the flattened Finding drops must
	// still be recoverable from RawCliOutput.
	var raw struct {
		Results []struct {
			Extra struct {
				Fingerprint string `json:"fingerprint"`
				Metadata    struct {
					CWE []string `json:"cwe"`
				} `json:"metadata"`
			} `json:"extra"`
		} `json:"results"`
	}
	if err := json.Unmarshal(res.RawCliOutput, &raw); err != nil {
		t.Fatalf("re-decoding RawCliOutput: %v", err)
	}
	if len(raw.Results) != 1 {
		t.Fatalf("want 1 result in RawCliOutput, got %d", len(raw.Results))
	}
	if got := raw.Results[0].Extra.Fingerprint; got != "abc123def456" {
		t.Fatalf("fingerprint = %q, want abc123def456", got)
	}
	if len(raw.Results[0].Extra.Metadata.CWE) != 1 || raw.Results[0].Extra.Metadata.CWE[0] != "CWE-798" {
		t.Fatalf("metadata.cwe = %v, want [CWE-798]", raw.Results[0].Extra.Metadata.CWE)
	}
}

func TestParseCliOutput_ScanResultMarshalsRawCliOutput(t *testing.T) {
	res, err := parseCliOutput([]byte(sampleCliOutput))
	if err != nil {
		t.Fatalf("parseCliOutput: %v", err)
	}

	body, err := json.Marshal(res)
	if err != nil {
		t.Fatalf("marshal ScanResult: %v", err)
	}
	var wire map[string]json.RawMessage
	if err := json.Unmarshal(body, &wire); err != nil {
		t.Fatalf("unmarshal wire: %v", err)
	}
	if _, ok := wire["raw_cli_output"]; !ok {
		t.Fatalf("marshaled ScanResult missing raw_cli_output field: %s", body)
	}
	if _, ok := wire["findings"]; !ok {
		t.Fatalf("marshaled ScanResult missing findings field: %s", body)
	}
}

func TestParseCliOutput_InvalidJSON(t *testing.T) {
	if _, err := parseCliOutput([]byte("not json")); err == nil {
		t.Fatal("want error for invalid cli_output, got nil")
	}
}
