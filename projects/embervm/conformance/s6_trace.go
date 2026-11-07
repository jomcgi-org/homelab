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
	"sort"
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
// S6_TLC_JAR, S6_TLC_SPEC_DIR all set). The dev-only runner image carries the
// pinned JRE, tla2tools.jar and adoption_trace.tla under /opt/tla (BUILD
// :tla_tar_amd64). conformance.s6.enabled renders that env together with a
// /tmp emptyDir for the work dir and JVM-sized memory (chart values
// conformance.s6); any run without the env withholds the verdict as
// incomplete rather than claiming anything about the window.

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

// tlaBool renders a TLA+ boolean literal. Go's "true"/"false" are not TLA+:
// TLC would reject a generated window carrying them, so the S6 exporter must
// speak the model's vocabulary (TRUE/FALSE, as in the committed fixtures).
func tlaBool(value bool) string {
	if value {
		return "TRUE"
	}
	return "FALSE"
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
		"had_vm |-> " + tlaBool(s6Bool(vars, "had_vm")),
		"gate |-> " + tlaBool(s6Bool(vars, "gate")),
		"node_confirmed |-> " + tlaBool(s6Bool(vars, "node_confirmed")),
		"confirmed_by |-> " + tlaString(s6String(vars, "confirmed_by")),
	}
	return "[" + strings.Join(fields, ", ") + "]"
}

// writeS6TraceModule serializes a window as a TLA+ module instancing the
// shipped adoption_trace.tla with the concrete exported records, plus the
// scalar-only config that checks it.
//
// The window cannot ride in a .cfg the way the committed fixtures' scalar
// Fixture selector does: TLC configuration files accept only scalars, sets
// of scalars, and model values, never tuples or records (the driver fails
// anything richer with "expecting = or <-"). So the window lives in this
// generated module and plugs into the shipped spec by INSTANCE substitution
// (Fixture <- "live", LiveTrace <- the window); the .cfg stays scalar-only.
// The invariant aliases keep their bare names so a TLC rejection still reads
// "Invariant <Name> is violated" for the fail verdict.
func writeS6TraceModule(records []traceRecord, minEvents int, module, cfg *strings.Builder) {
	module.WriteString("---- MODULE window ----\nEXTENDS Naturals, Sequences, FiniteSets\nVARIABLES cursor, pending, completed, transitionsValid\nW ==\n    <<")
	for i, record := range records {
		if i > 0 {
			module.WriteString(",")
		}
		module.WriteString("\n      " + s6TraceRecordConstant(record))
	}
	module.WriteString(">>\nAT == INSTANCE adoption_trace WITH Fixture <- \"live\", LiveTrace <- W, MinTraceEvents <- " + strconv.Itoa(minEvents) + ", cursor <- cursor, pending <- pending, completed <- completed, transitionsValid <- transitionsValid\n")
	module.WriteString("Spec == AT!Spec\nTraceWellFormed == AT!TraceWellFormed\nNoDestroyBeforeConfirm == AT!NoDestroyBeforeConfirm\nNoDoubleAssign == AT!NoDoubleAssign\nDestroyTransitionsConform == AT!DestroyTransitionsConform\n================================================================================\n")
	cfg.WriteString("SPECIFICATION Spec\n\nCHECK_DEADLOCK FALSE\n\nINVARIANT TraceWellFormed\nINVARIANT NoDestroyBeforeConfirm\nINVARIANT NoDoubleAssign\nINVARIANT DestroyTransitionsConform\n")
}

// s6TLCConfigured reports whether the TLC leg can run here. The toolchain
// (Temurin java, tla2tools jar, a directory carrying adoption_trace.tla)
// ships in the runner image under /opt/tla, but only the chart's S6_TLC_*
// env (conformance.s6.enabled) points the runner at it.
func s6TLCConfigured(cfg config) bool {
	return cfg.tlcJava != "" && cfg.tlcJar != "" && cfg.tlcSpecDir != ""
}

// runS6TLC stages adoption_trace.tla plus the generated window module and
// its scalar-only config into a temp dir and runs TLC directly, mirroring
// the tlc.sh model-check block. Output is classified by
// classifyS6TLCOutput; the java exit code is ignored because a violation
// exit and a crash exit both need output text to tell apart, and only named
// violations may read as fail.
func runS6TLC(ctx context.Context, cfg config, records []traceRecord) (verdict, detail string) {
	verdict, detail, _ = runS6TLCWithOutput(ctx, cfg, records)
	return verdict, detail
}

// runS6TLCWithOutput is runS6TLC plus the captured TLC output. The production
// path ignores the third value; the end-to-end test (s6_tlc_test.go) asserts
// on it directly, so a pass is proven exhaustive by the queue-drained line
// itself rather than only by the classified verdict.
func runS6TLCWithOutput(ctx context.Context, cfg config, records []traceRecord) (verdict, detail, tlcOutput string) {
	coverage := len(records)
	work, err := os.MkdirTemp("", "s6-trace-")
	if err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage), ""
	}
	defer os.RemoveAll(work)

	for _, name := range []string{"adoption_trace.tla", "destroy_contract.tla"} {
		spec, err := os.ReadFile(filepath.Join(cfg.tlcSpecDir, name))
		if err != nil {
			return verdictIncomplete, fmt.Sprintf("tlc spec unreadable: %v (coverage=%d)", err, coverage), ""
		}
		if err := os.WriteFile(filepath.Join(work, name), spec, 0o600); err != nil {
			return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage), ""
		}
	}
	var moduleText, cfgText strings.Builder
	writeS6TraceModule(records, cfg.minTraceEvents, &moduleText, &cfgText)
	if err := os.WriteFile(filepath.Join(work, "window.tla"), []byte(moduleText.String()), 0o600); err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage), ""
	}
	if err := os.WriteFile(filepath.Join(work, "window.cfg"), []byte(cfgText.String()), 0o600); err != nil {
		return verdictIncomplete, fmt.Sprintf("tlc staging failed: %v (coverage=%d)", err, coverage), ""
	}

	timeoutCtx, cancel := context.WithTimeout(ctx, s6TLCTimeoutSeconds*time.Second)
	defer cancel()
	// -XX:-UsePerfData: the JVM otherwise maps an hsperfdata file under
	// /tmp, the chart's size-limited emptyDir; TLC needs no jstat counters.
	cmd := exec.CommandContext(timeoutCtx, cfg.tlcJava,
		"-XX:+UseParallelGC",
		"-XX:-UsePerfData",
		"-Dtlc2.TLC.stopAfter="+strconv.Itoa(s6TLCTimeoutSeconds),
		"-cp", cfg.tlcJar, "tlc2.TLC", "-workers", "auto",
		"-config", "window.cfg", "window")
	cmd.Dir = work
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	runErr := cmd.Run()
	verdict, detail = classifyS6TLCOutput(output.String(), coverage)
	if verdict == verdictPass && runErr != nil {
		return verdictIncomplete, fmt.Sprintf("tlc did not finish successfully: %v", runErr), output.String()
	}
	return verdict, detail, output.String()
}

func runS6(ctx context.Context, cfg config, client *controlPlaneClient, suiteStarted time.Time) scenarioVerdict {
	query := url.Values{
		"since_ts_ms": []string{fmt.Sprintf("%d", suiteStarted.UnixMilli())},
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
	records, reason := s6ObservedWindow(view)
	if reason != "" {
		return scenarioVerdict{Verdict: verdictIncomplete, Detail: reason}
	}
	if len(records) < cfg.minTraceEvents {
		return scenarioVerdict{Verdict: verdictVacuous, Detail: "thin adoption window after filtering other specs"}
	}
	if !s6TLCConfigured(cfg) {
		return scenarioVerdict{
			Verdict: verdictIncomplete,
			Detail:  fmt.Sprintf("tlc toolchain not configured; window of %d records staged for offline check, verdict withheld", len(view.Records)),
		}
	}
	verdict, detail := runS6TLC(ctx, cfg, records)
	if verdict == verdictPass {
		completion, reason := s6DestroyCoverage(records)
		if reason != "" {
			return scenarioVerdict{Verdict: verdictIncomplete, Detail: reason}
		}
		if completion == 0 {
			return scenarioVerdict{Verdict: verdictVacuous, Detail: "no completed eligible session destruction"}
		}
		detail += fmt.Sprintf("; destroy_contract/v1 observed completions=%d", completion)
	}
	return scenarioVerdict{Verdict: verdict, Detail: detail}
}

// Read every spec so drop markers and sequence gaps cannot hide behind the
// adoption filter. Sequence order is only meaningful within one CP incarnation.
// This checks the returned window; the debug writer cannot certify absence of
// unreported loss before/after it. PASS is scoped to observed complete segments.
func s6ObservedWindow(view traceWindowView) ([]traceRecord, string) {
	if view.RecordCount != len(view.Records) {
		return nil, "trace count mismatch"
	}
	if len(view.RunIDs) != 1 || view.RunIDs[0] == "" {
		return nil, "cross-run or missing trace identity; replay incomplete"
	}
	records := append([]traceRecord(nil), view.Records...)
	sort.Slice(records, func(i, j int) bool { return records[i].Seq < records[j].Seq })
	var adoption []traceRecord
	for i, record := range records {
		if record.RunID != view.RunIDs[0] || record.Seq <= 0 || (i > 0 && record.Seq != records[i-1].Seq+1) {
			return nil, "trace sequence gap or inconsistent incarnation"
		}
		if dropped, ok := record.Vars["spec_trace_dropped_before"]; ok && fmt.Sprint(dropped) != "0" {
			return nil, "trace reports dropped events"
		}
		if record.Spec != "adoption" {
			continue
		}
		if record.Action == "begin_destroy" || record.Action == "confirm_destroy" {
			gate, ok := record.Vars["gate"].(bool)
			if !ok || !gate {
				return nil, "unsupported destroy gate; replay incomplete"
			}
			if s6String(record.Vars, "session_id") == "" || s6String(record.Vars, "vm_id") == "" {
				return nil, "missing destroy identity; replay incomplete"
			}
			if record.Action == "confirm_destroy" {
				hadVM, ok := record.Vars["had_vm"].(bool)
				if !ok || !hadVM {
					return nil, "nonresident destroy is outside the observed contract"
				}
				if _, ok := record.Vars["node_confirmed"].(bool); !ok {
					return nil, "missing typed confirmation evidence"
				}
				if reason := s6String(record.Vars, "confirmed_by"); reason != "teardown" && reason != "absence" && reason != "" {
					return nil, "unsupported destruction evidence: " + reason
				}
			}
		}
		adoption = append(adoption, record)
	}
	// A confirmation with no observed intent may start before the window or
	// follow a residency rebind. Neither establishes a production violation.
	begun := map[[2]string]bool{}
	for _, record := range adoption {
		if record.Action == "begin_destroy" {
			begun[s6DestroyKey(record)] = true
		}
	}
	for _, record := range adoption {
		if record.Action == "confirm_destroy" && s6Bool(record.Vars, "node_confirmed") && !begun[s6DestroyKey(record)] {
			return nil, "confirmation lacks an observed matching intent; partial window or residency rebind"
		}
	}
	return adoption, ""
}

func s6DestroyKey(record traceRecord) [2]string {
	return [2]string{s6String(record.Vars, "session_id"), s6String(record.Vars, "vm_id")}
}

// Called only after TLC accepts every transition. Counting is coverage, not a
// second implementation of the transition checker.
func s6DestroyCoverage(records []traceRecord) (int, string) {
	pending := map[[2]string]bool{}
	completed := 0
	for _, record := range records {
		switch record.Action {
		case "begin_destroy":
			pending[s6DestroyKey(record)] = true
		case "confirm_destroy":
			delete(pending, s6DestroyKey(record))
			completed++
		}
	}
	if len(pending) > 0 {
		return completed, "open destruction intent; window incomplete"
	}
	return completed, ""
}
