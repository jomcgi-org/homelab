package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// S6: TLC trace validation of adoption.tla against exported SpecTrace windows
// (issue #6415). Staged default-off behind S6_ENABLED (chart key
// conformance.s6.enabled, false in every environment): when disabled S6 does
// not run at all and the scenario list is exactly S1..S5.
//
// Verdict contract, same as Tier A plus the truncated-run rule from #4050:
//   - empty or thin window -> vacuous, never pass (checked BEFORE any TLC);
//   - TLC rejects the window -> fail naming the violated invariant;
//   - TLC accepts exhaustively -> pass with coverage = window length;
//   - TLC truncated (no "0 states left on queue" line) or toolchain missing
//     -> incomplete, never pass.
//
// The TLC leg runs only where the toolchain is configured (S6_TLC_JAVA,
// S6_TLC_JAR, S6_TLC_SPEC_DIR all set): the dev scheduled-job context. The
// shipped runner image carries no JVM, so in-cluster S6 withholds the verdict
// as incomplete rather than claiming anything about the window.

const (
	s6ScenarioID      = "S6"
	s6TraceWindowPath = "/v1/conformance/trace-window"
	// TLC runs single-threaded output scan; keep the wall bound aligned with
	// the tlc.sh driver so CI fixtures and live runs share one truncation
	// definition.
	s6TLCTimeoutSeconds = 600
)

type traceRecord struct {
	RunID  string         `json:"run_id"`
	Seq    int64          `json:"seq"`
	Mono   int64          `json:"mono"`
	TS     int64          `json:"ts"`
	Spec   string         `json:"spec"`
	Action string         `json:"action"`
	Vars   map[string]any `json:"vars"`
}

type traceWindowView struct {
	Enabled     bool          `json:"enabled"`
	RunIDs      []string      `json:"run_ids"`
	RecordCount int           `json:"record_count"`
	Records     []traceRecord `json:"records"`
}

var violatedInvariantPattern = regexp.MustCompile(`Invariant ([A-Za-z0-9_]+) is violated`)

// extractViolatedInvariant names the invariant TLC rejected, or "" when the
// output names no violation (a crash and a clean run both name none).
func extractViolatedInvariant(tlcOutput string) string {
	match := violatedInvariantPattern.FindStringSubmatch(tlcOutput)
	if match == nil {
		return ""
	}
	return match[1]
}

// tlcQueueDrained reports the exhaustive-completion line the tlc.sh rule from
// #4050 requires: a zero exit without it is a truncated search, not a proof.
func tlcQueueDrained(tlcOutput string) bool {
	matched, _ := regexp.MatchString(`states generated, .*, 0 states left on queue`, tlcOutput)
	return matched
}

// classifyS6TLCOutput maps captured TLC stdout to the S6 verdict triple plus
// incomplete. Coverage is the checked window length, so a pass always carries
// what it checked; there is no zero-coverage pass because thin windows never
// reach this function.
func classifyS6TLCOutput(tlcOutput string, coverage int) (verdict, detail string) {
	if invariant := extractViolatedInvariant(tlcOutput); invariant != "" {
		return verdictFail, fmt.Sprintf("tlc rejects window: %s (coverage=%d)", invariant, coverage)
	}
	if tlcQueueDrained(tlcOutput) {
		return verdictPass, fmt.Sprintf("tlc accepts window exhaustively (coverage=%d)", coverage)
	}
	return verdictIncomplete, fmt.Sprintf("tlc truncated: no queue-drained line, verdict withheld (coverage=%d)", coverage)
}

func s6String(vars map[string]any, key string) string {
	value, _ := vars[key].(string)
	return value
}

func s6Bool(vars map[string]any, key string) bool {
	value, _ := vars[key].(bool)
	return value
}

// tlaString renders a TLA+ double-quoted string literal.
func tlaString(value string) string {
	var builder strings.Builder
	builder.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"':
			builder.WriteString(`\"`)
		case '\\':
			builder.WriteString(`\\`)
		default:
			builder.WriteRune(r)
		}
	}
	builder.WriteByte('"')
	return builder.String()
}

// s6TraceRecordConstant renders one window record as the uniform eight-field
// TLA+ record adoption_trace.tla replays. Fields the action did not emit take
// neutral defaults so field access stays total; only confirm_destroy records
// are ever evaluable, so the defaults cannot fabricate intent or confirmation.
func s6TraceRecordConstant(record traceRecord) string {
	vars := record.Vars
	if vars == nil {
		vars = map[string]any{}
	}
	fields := []string{
		"action |-> " + tlaString(record.Action),
		"vm |-> " + tlaString(s6String(vars, "vm_id")),
		"node |-> " + tlaString(s6String(vars, "node_id")),
		"session |-> " + tlaString(s6String(vars, "session_id")),
		"had_vm |-> " + strconv.FormatBool(s6Bool(vars, "had_vm")),
		"gate |-> " + strconv.FormatBool(s6Bool(vars, "gate")),
		"node_confirmed |-> " + strconv.FormatBool(s6Bool(vars, "node_confirmed")),
		"confirmed_by |-> " + tlaString(s6String(vars, "confirmed_by")),
	}
	return "[" + strings.Join(fields, ", ") + "]"
}

// writeS6TraceCFG serializes a window as a TLC config pairing the shipped
// adoption_trace.tla with a concrete Trace constant, the same shape as the
// committed fixture cfgs under projects/embervm/specs.
func writeS6TraceCFG(records []traceRecord, minEvents int, builder *strings.Builder) {
	builder.WriteString("SPECIFICATION Spec\n\nCHECK_DEADLOCK FALSE\n\nCONSTANTS\n")
	builder.WriteString("    MinTraceEvents = " + strconv.Itoa(minEvents) + "\n")
	builder.WriteString("    Trace = <<")
	for i, record := range records {
		if i > 0 {
			builder.WriteString(",")
		}
		builder.WriteString("\n        " + s6TraceRecordConstant(record))
	}
	builder.WriteString("\n    >>\n\nINVARIANT TraceWellFormed\nINVARIANT NoDestroyBeforeConfirm\nINVARIANT NoDoubleAssign\n")
}

// s6TLCConfigured reports whether the TLC leg can run here. The toolchain
// (Temurin java, tla2tools jar, a directory carrying adoption_trace.tla)
// exists only in the dev scheduled-job context, never in the runner image.
func s6TLCConfigured(cfg config) bool {
	return cfg.tlcJava != "" && cfg.tlcJar != "" && cfg.tlcSpecDir != ""
}

// runS6TLC stages adoption_trace.tla plus the generated window cfg into a
// temp dir and runs TLC directly, mirroring the tlc.sh model-check block.
// Output is classified by classifyS6TLCOutput; the java exit code is ignored
// because a violation exit and a crash exit both need output text to tell
// apart, and only named violations may read as fail.
func runS6TLC(ctx context.Context, cfg config, records []traceRecord) (verdict, detail string) {
	coverage := len(records)
	work, err := os.MkdirTemp("", "s6-trace-")
	if err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage)
	}
	defer os.RemoveAll(work)

	spec, err := os.ReadFile(filepath.Join(cfg.tlcSpecDir, "adoption_trace.tla"))
	if err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc spec unreadable: %v (coverage=%d)", err, coverage)
	}
	if err := os.WriteFile(filepath.Join(work, "adoption_trace.tla"), spec, 0o600); err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage)
	}
	var cfgText strings.Builder
	writeS6TraceCFG(records, cfg.minTraceEvents, &cfgText)
	cfgName := "window.cfg"
	if err := os.WriteFile(filepath.Join(work, cfgName), []byte(cfgText.String()), 0o600); err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage)
	}

	timeoutCtx, cancel := context.WithTimeout(ctx, s6TLCTimeoutSeconds*time.Second)
	defer cancel()
	cmd := exec.CommandContext(timeoutCtx, cfg.tlcJava,
		"-XX:+UseParallelGC",
		"-Dtlc2.TLC.stopAfter="+strconv.Itoa(s6TLCTimeoutSeconds),
		"-cp", cfg.tlcJar, "tlc2.TLC", "-workers", "auto",
		"-config", cfgName, "adoption_trace")
	cmd.Dir = work
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	_ = cmd.Run()
	return classifyS6TLCOutput(output.String(), coverage)
}

func runS6(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time) scenarioVerdict {
	query := url.Values{
		"since_ts_ms": []string{fmt.Sprintf("%d", suiteStarted.UnixMilli())},
		"spec":        []string{"adoption"},
	}
	path := s6TraceWindowPath + "?" + query.Encode()
	response, err := client.request(ctx, http.MethodGet, path, nil, "", nil)
	if err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: fmt.Sprintf("GET %s: %v", path, err)}
	}
	if response.status != http.StatusOK {
		return scenarioVerdict{Verdict: verdictFail, Detail: httpErrorDetail(http.MethodGet, path, response)}
	}
	var view traceWindowView
	if err := json.Unmarshal(response.body, &view); err != nil {
		return scenarioVerdict{Verdict: verdictFail, Detail: "invalid trace-window response: " + err.Error()}
	}
	if !view.Enabled {
		return scenarioVerdict{Verdict: verdictVacuous, Detail: "trace gate disabled"}
	}
	// Vacuity first: a thin window satisfies every ordering predicate, so it
	// must never reach TLC where it would read as pass.
	if len(view.Records) < cfg.minTraceEvents {
		return scenarioVerdict{
			Verdict: verdictVacuous,
			Detail:  fmt.Sprintf("thin window: %d records below threshold %d, never pass", len(view.Records), cfg.minTraceEvents),
		}
	}
	if !s6TLCConfigured(cfg) {
		return scenarioVerdict{
			Verdict: verdictIncomplete,
			Detail:  fmt.Sprintf("tlc toolchain not configured; window of %d records staged for offline check, verdict withheld", len(view.Records)),
		}
	}
	verdict, detail := runS6TLC(ctx, cfg, view.Records)
	return scenarioVerdict{Verdict: verdict, Detail: detail}
}
