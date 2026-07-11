// Package cliout parses standard `semgrep --json` cli_output into the
// vsockproto wire shape, shared by both scan paths: the warm single-file
// scan-server (internal/scandriver) and the whole-tree interfile scan
// (internal/fullscan).
package cliout

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// cliOutput is the shape of standard `semgrep --json` cli_output. Severity is
// already the ERROR/WARNING/INFO string (no LSP 1-4 mapping); start.line/col
// are 1-based.
type cliOutput struct {
	Results []struct {
		CheckID string `json:"check_id"`
		Path    string `json:"path"`
		Start   struct {
			Line int `json:"line"`
			Col  int `json:"col"`
		} `json:"start"`
		Extra struct {
			Message  string `json:"message"`
			Severity string `json:"severity"`
		} `json:"extra"`
	} `json:"results"`
	Errors []struct {
		Message string `json:"message"`
	} `json:"errors"`
}

// Parse decodes one line (or blob) of `semgrep --json` cli_output into a
// vsockproto.ScanResult: it flattens Results into vsockproto.Finding (the
// legacy, backward-compatible shape) and also preserves the verbatim input
// bytes in RawCliOutput, so consumers that need full match metadata
// (fingerprints, end positions, dataflow) are not limited to the flattened
// fields. line is copied into RawCliOutput as-is (the caller owns it after
// this call, so pass a copy if the source buffer may be reused).
//
// If stripPrefix is non-empty, each flattened Finding's Path is rewritten
// from a stripPrefix-rooted absolute path back to repo-relative, by trimming
// stripPrefix and any leading path separator. A path that does not have the
// prefix is left unchanged. This rewrite only touches the flattened
// Findings; RawCliOutput always preserves the verbatim original bytes.
func Parse(line []byte, stripPrefix string) (vsockproto.ScanResult, error) {
	var out cliOutput
	if err := json.Unmarshal(line, &out); err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("decode cli_output: %w", err)
	}

	var res vsockproto.ScanResult
	res.RawCliOutput = json.RawMessage(line)
	for _, r := range out.Results {
		res.Findings = append(res.Findings, vsockproto.Finding{
			Path:     stripPathPrefix(r.Path, stripPrefix),
			Line:     r.Start.Line,
			Col:      r.Start.Col,
			RuleID:   r.CheckID,
			Severity: r.Extra.Severity,
			Message:  r.Extra.Message,
		})
	}
	for _, e := range out.Errors {
		if e.Message != "" {
			res.Errors = append(res.Errors, e.Message)
		}
	}
	return res, nil
}

// stripPathPrefix trims prefix (and one leading path separator) from path. If
// prefix is empty or path does not have that prefix, path is returned as-is.
func stripPathPrefix(path, prefix string) string {
	if prefix == "" || !strings.HasPrefix(path, prefix) {
		return path
	}
	trimmed := strings.TrimPrefix(path, prefix)
	trimmed = strings.TrimPrefix(trimmed, string(os.PathSeparator))
	return trimmed
}
