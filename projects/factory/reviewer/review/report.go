package review

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// Check run conclusions the reviewer posts. Anything that is not a clean
// pass fails; neutral and skipped are never used, since GitHub counts both as
// passing a required check.
const (
	ConclusionSuccess   = "success"
	ConclusionFailure   = "failure"
	ConclusionCancelled = "cancelled"
)

// Severities, most severe first. Critical, high and medium block; low never
// does.
const (
	SeverityCritical = "critical"
	SeverityHigh     = "high"
	SeverityMedium   = "medium"
	SeverityLow      = "low"
)

// Severities lists every severity, most severe first.
var Severities = []string{SeverityCritical, SeverityHigh, SeverityMedium, SeverityLow}

// Statuses of a previous round's finding, as the agent accounts for it.
const (
	StatusFixed     = "fixed"
	StatusJustified = "justified"
	StatusOpen      = "open"
)

// Report is the reviewer agent's answer: findings only. The service decides
// the conclusion from them; any verdict the agent declares is ignored.
type Report struct {
	Summary  string     `json:"summary"`
	Findings []Finding  `json:"findings"`
	Previous []Previous `json:"previous"`
}

// Finding is one review finding with an ID that stays stable across
// rounds: `<path>:<rule>` (REVIEW.md "Report").
type Finding struct {
	ID       string `json:"id"`
	Severity string `json:"severity"`
	Path     string `json:"path,omitempty"`
	Line     int    `json:"line,omitempty"`
	Title    string `json:"title"`
	Detail   string `json:"detail,omitempty"`
	Defer    bool   `json:"defer,omitempty"`
}

// Previous is the agent's account of a previous round's blocking finding.
type Previous struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	Note   string `json:"note"`
}

func blocks(severity string) bool { return severity != SeverityLow }

func rank(severity string) int {
	for i, s := range Severities {
		if s == severity {
			return i
		}
	}
	return len(Severities)
}

// maxReply bounds how much of a reply is searched for the report.
const maxReply = 1 << 20

var fence = regexp.MustCompile("(?s)```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)\r?\n[ \t]*```")

// ContractRules are the rule names REVIEW.md "Report" defines; with the
// configured scoped rule names they bound the findings metrics' labels.
var ContractRules = []string{
	"correctness", "security", "boundary", "reachability", "validation", "rollout", "goal", "gate",
	"unverified", "unreviewed", "review-injection", HumanGateID,
}

// ruleOf is a finding ID's rule, the part after its last ':'.
func ruleOf(id string) string {
	if i := strings.LastIndex(id, ":"); i >= 0 {
		id = id[i+1:]
	}
	return strings.ToLower(strings.TrimSpace(id))
}

// isGate reports a finding only Joe's approval of the head clears.
func isGate(f Finding) bool { return ruleOf(f.ID) == HumanGateID }

// ParseReport reads the last fenced block of the agent's reply as a report.
// No block, a block in another language, invalid JSON, a report without a
// summary or a findings list, an unknown severity or a finding without a
// title or detail is an error, never a pass. A missing ID is derived from
// the path and title; an unknown previous status counts as open.
func ParseReport(reply string) (*Report, error) {
	if len(reply) > maxReply {
		reply = reply[len(reply)-maxReply:]
	}
	blocks := fence.FindAllStringSubmatch(reply, -1)
	if len(blocks) == 0 {
		return nil, errors.New("the reply has no fenced JSON block")
	}
	last := blocks[len(blocks)-1]
	if lang := strings.ToLower(last[1]); lang != "" && lang != "json" {
		return nil, fmt.Errorf("the last fenced block is %q, not JSON", clip(lang, 40))
	}
	dec := json.NewDecoder(strings.NewReader(last[2]))
	var r Report
	if err := dec.Decode(&r); err != nil {
		return nil, fmt.Errorf("the last fenced block is not a findings report: %w", err)
	}
	if dec.More() {
		return nil, errors.New("the last fenced block holds more than one JSON value")
	}
	var probe struct {
		Findings json.RawMessage `json:"findings"`
	}
	if json.Unmarshal([]byte(last[2]), &probe) != nil || probe.Findings == nil || string(probe.Findings) == "null" {
		return nil, errors.New("the report has no findings list")
	}
	if strings.TrimSpace(r.Summary) == "" {
		return nil, errors.New("the report has no summary")
	}
	for i := range r.Findings {
		f := &r.Findings[i]
		f.Severity = strings.ToLower(strings.TrimSpace(f.Severity))
		if rank(f.Severity) == len(Severities) {
			return nil, fmt.Errorf("finding %d has severity %q, not one of %s", i+1, clip(f.Severity, 40), strings.Join(Severities, ", "))
		}
		f.Title, f.Detail = strings.TrimSpace(f.Title), strings.TrimSpace(f.Detail)
		if f.Title == "" {
			f.Title, _, _ = strings.Cut(f.Detail, "\n")
		}
		if f.Title == "" {
			return nil, fmt.Errorf("finding %d has no title or detail", i+1)
		}
		f.ID = strings.TrimSpace(f.ID)
		if f.ID == "" {
			f.ID = derivedID(f)
		}
	}
	for i := range r.Previous {
		p := &r.Previous[i]
		p.ID = strings.TrimSpace(p.ID)
		p.Status = strings.ToLower(strings.TrimSpace(p.Status))
		if p.Status != StatusFixed && p.Status != StatusJustified {
			p.Status = StatusOpen
		}
	}
	return &r, nil
}

var slugJunk = regexp.MustCompile(`[^a-z0-9]+`)

func derivedID(f *Finding) string {
	slug := strings.Trim(slugJunk.ReplaceAllString(strings.ToLower(f.Title), "-"), "-")
	return clip(strings.ToLower(f.Path)+":"+slug, 120)
}

// Blocked is a finding that blocks the check, or a justified medium that no
// longer does.
type Blocked struct {
	Finding
	// Carried marks a previous round's finding the agent did not report
	// again; Repeated one it reported again.
	Carried  bool
	Repeated bool
	// Note is the agent's note on a previous finding.
	Note string
	// Reason says why a previous finding still blocks.
	Reason string
}

// Assessment is the service's deterministic reading of a report against the
// previous round's blocking findings.
type Assessment struct {
	Conclusion string
	Blocking   []Blocked
	Accepted   []Blocked
	Fixed      []Finding
	// Withdrawn are previous findings the agent withdrew as mistaken.
	Withdrawn []Blocked
	// Approved are human-gate findings Approver cleared on this head.
	Approved []Finding
	Approver string
	Optional []Finding
	Deferred []Finding
	// DroppedLow counts low findings past MaxLow.
	DroppedLow int
}

// withdrawn reports a note withdrawing a previous finding.
func withdrawn(note string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "withdrawn")
}

// MaxLow is how many low findings a check run lists.
const MaxLow = 5

// Assess decides a review: success only when no critical, high or medium
// finding blocks. A previous blocking finding is resolved when the agent
// reports it fixed, or marks it justified with a note starting "withdrawn"
// (any severity: the finding was wrong); a medium one is also accepted when
// justified with any note. Anything else (open, unaccounted for, a justified
// critical or high, a justification without a note) still blocks.
func Assess(r *Report, prior []Finding) Assessment {
	var a Assessment
	status := map[string]Previous{}
	for _, p := range r.Previous {
		if _, ok := status[p.ID]; !ok && p.ID != "" {
			status[p.ID] = p
		}
	}
	priorByID := map[string]Finding{}
	for _, p := range prior {
		priorByID[p.ID] = p
	}
	justified := func(id string) (string, bool) {
		p, wasPrior := priorByID[id]
		s, ok := status[id]
		note := strings.TrimSpace(s.Note)
		return note, wasPrior && ok && p.Severity == SeverityMedium && s.Status == StatusJustified && note != ""
	}
	repeated := func(f Finding) Blocked {
		_, was := priorByID[f.ID]
		return Blocked{Finding: f, Repeated: was, Note: strings.TrimSpace(status[f.ID].Note)}
	}
	current := map[string]bool{}
	low := 0
	for _, f := range r.Findings {
		current[f.ID] = true
		switch {
		case f.Severity == SeverityLow:
			if low++; low > MaxLow {
				a.DroppedLow++
			} else if f.Defer {
				a.Deferred = append(a.Deferred, f)
			} else {
				a.Optional = append(a.Optional, f)
			}
		case f.Severity == SeverityMedium:
			if note, ok := justified(f.ID); ok {
				a.Accepted = append(a.Accepted, Blocked{Finding: f, Repeated: true, Note: note})
			} else {
				a.Blocking = append(a.Blocking, repeated(f))
			}
		default:
			a.Blocking = append(a.Blocking, repeated(f))
		}
	}
	for _, p := range prior {
		if current[p.ID] {
			continue // reported again and judged above
		}
		s, ok := status[p.ID]
		note := strings.TrimSpace(s.Note)
		if ok && s.Status == StatusJustified && withdrawn(note) {
			a.Withdrawn = append(a.Withdrawn, Blocked{Finding: p, Carried: true, Note: note})
			continue
		}
		if _, accepted := justified(p.ID); accepted {
			a.Accepted = append(a.Accepted, Blocked{Finding: p, Carried: true, Note: note})
			continue
		}
		b := Blocked{Finding: p, Carried: true, Note: note}
		switch {
		case !ok:
			b.Reason = "not accounted for"
		case s.Status == StatusFixed:
			a.Fixed = append(a.Fixed, p)
			continue
		case s.Status == StatusJustified && p.Severity != SeverityMedium:
			b.Reason = "justified, but only a medium finding can be justified"
		case s.Status == StatusJustified:
			b.Reason = "justified without a note"
		default:
			b.Reason = "still open"
		}
		a.Blocking = append(a.Blocking, b)
	}
	sortBlocked(a.Blocking)
	a.Conclusion = ConclusionSuccess
	if len(a.Blocking) > 0 {
		a.Conclusion = ConclusionFailure
	}
	return a
}

func sortBlocked(bs []Blocked) {
	// Stable insertion sort by severity keeps the agent's order within one.
	for i := 1; i < len(bs); i++ {
		for j := i; j > 0 && rank(bs[j].Severity) < rank(bs[j-1].Severity); j-- {
			bs[j], bs[j-1] = bs[j-1], bs[j]
		}
	}
}

// Outcome is how a review's check run completes.
type Outcome struct {
	Conclusion string
	Output     github.CheckRunOutput
}

// Output limits. GitHub takes 65535 characters in a summary or text.
const (
	maxSummary      = 60000
	maxAgentSummary = 4000
	maxTitle        = 300
	maxDetail       = 1500
	maxMessage      = 4000
	maxStored       = 40
	maxStoredDetail = 500
)

// enqueuedMarker records on a review check run that its head was enqueued
// once. The state marker holds a completed review's State for the next
// round. gateOnlyMarker marks a check whose only blocking finding is the
// human gate, which no revision round can fix. All are stripped from
// anything the agent wrote.
const (
	enqueuedMarker = "<!-- factory-review:enqueued -->"
	gateOnlyMarker = "<!-- factory-review:human-gate-only -->"
	statePrefix    = "<!-- factory-review:state v1 "
	stateSuffix    = " -->"
)

var stateRE = regexp.MustCompile(`<!-- factory-review:state v1 ([A-Za-z0-9+/=]*) -->`)

// State is what a completed review keeps on its check run: the blocking
// findings the next round must account for (never the human gate, which
// only a person clears) and the head's human gate.
type State struct {
	Blocking []Finding `json:"blocking"`
	Gate     *Gate     `json:"gate,omitempty"`
}

// Render writes an assessment as the check run's output. The summary lists
// blocking findings first, then accepted, withdrawn, fixed, optional and
// deferred ones; annotations mark this round's findings; the text carries
// the State for the next round in a hidden block.
func Render(a Assessment, r *Report, gate *Gate, footer string) Outcome {
	o := Outcome{Conclusion: a.Conclusion}
	optional := len(a.Optional) + len(a.Deferred) + a.DroppedLow
	if n := len(a.Blocking); n > 0 {
		counts := map[string]int{}
		for _, b := range a.Blocking {
			counts[b.Severity]++
		}
		var parts []string
		for _, s := range Severities {
			if counts[s] > 0 {
				parts = append(parts, fmt.Sprintf("%d %s", counts[s], s))
			}
		}
		o.Output.Title = fmt.Sprintf("%d blocking %s (%s)", n, plural(n, "finding"), strings.Join(parts, ", "))
	} else {
		o.Output.Title = "No blocking findings"
		if optional > 0 {
			o.Output.Title += fmt.Sprintf(", %d optional", optional)
		}
	}

	var s strings.Builder
	if sum := strings.TrimSpace(untrusted(r.Summary)); sum != "" {
		s.WriteString(clip(sum, maxAgentSummary))
		s.WriteString("\n\n")
	}
	if len(a.Blocking) > 0 {
		s.WriteString("### Blocking\n\n")
		for _, b := range a.Blocking {
			writeFinding(&s, b.Finding)
			switch {
			case b.Carried:
				fmt.Fprintf(&s, "  From the previous review: %s.", b.Reason)
			case b.Repeated:
				s.WriteString("  Raised in the previous review and still present.")
			}
			if b.Note != "" {
				fmt.Fprintf(&s, " Note: %s", oneLine(b.Note, maxMessage))
			}
			s.WriteString("\n")
		}
		s.WriteString("\n")
	}
	if len(a.Withdrawn) > 0 {
		s.WriteString("### Withdrawn\n\nPrevious findings the reviewer withdrew as mistaken:\n\n")
		for _, b := range a.Withdrawn {
			writeFinding(&s, b.Finding)
			fmt.Fprintf(&s, "  Note: %s\n", oneLine(b.Note, maxMessage))
		}
		s.WriteString("\n")
	}
	if len(a.Approved) > 0 {
		fmt.Fprintf(&s, "### Approved by %s\n\n", a.Approver)
		for _, f := range a.Approved {
			writeFinding(&s, f)
		}
		s.WriteString("\n")
	}
	if len(a.Accepted) > 0 {
		s.WriteString("### Accepted\n\nMedium findings from the previous review, justified by the agent:\n\n")
		for _, b := range a.Accepted {
			writeFinding(&s, b.Finding)
			fmt.Fprintf(&s, "  Justification: %s\n", oneLine(b.Note, maxMessage))
		}
		s.WriteString("\n")
	}
	if len(a.Fixed) > 0 {
		s.WriteString("### Fixed since the previous review\n\n")
		for _, f := range a.Fixed {
			fmt.Fprintf(&s, "- `%s`: %s\n", oneLine(f.ID, maxTitle), oneLine(f.Title, maxTitle))
		}
		s.WriteString("\n")
	}
	if len(a.Optional) > 0 || a.DroppedLow > 0 {
		s.WriteString("### Optional\n\n")
		for _, f := range a.Optional {
			writeFinding(&s, f)
		}
		if a.DroppedLow > 0 {
			fmt.Fprintf(&s, "\n%d more low %s dropped; at most %d are listed.\n", a.DroppedLow, plural(a.DroppedLow, "finding was", "findings were"), MaxLow)
		}
		s.WriteString("\n")
	}
	if len(a.Deferred) > 0 {
		s.WriteString("### Deferred\n\n")
		for _, f := range a.Deferred {
			writeFinding(&s, f)
		}
		s.WriteString("\n")
	}
	if len(a.Blocking)+len(a.Accepted)+len(a.Withdrawn)+len(a.Fixed)+len(a.Approved)+optional == 0 {
		s.WriteString("No findings.\n\n")
	}
	if len(a.Blocking) > 0 && !slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return !isGate(b.Finding) }) {
		s.WriteString(gateOnlyMarker)
	}
	s.WriteString(footer)
	o.Output.Summary = clip(s.String(), maxSummary)

	var anns []github.Annotation
	add := func(f Finding) {
		p := cleanPath(f.Path)
		if p == "" || len(anns) >= github.MaxAnnotations {
			return
		}
		line := max(f.Line, 1)
		level := map[string]string{SeverityCritical: "failure", SeverityHigh: "failure", SeverityMedium: "warning", SeverityLow: "notice"}[f.Severity]
		msg := f.Title
		if f.Detail != "" {
			msg += "\n\n" + f.Detail
		}
		anns = append(anns, github.Annotation{
			Path: p, StartLine: line, EndLine: line, Level: level,
			Title:   clip(f.Severity+": "+oneLine(f.Title, maxTitle), maxTitle),
			Message: clip(untrusted(msg), maxMessage),
		})
	}
	for _, b := range a.Blocking {
		if !b.Carried {
			add(b.Finding)
		}
	}
	for _, b := range a.Accepted {
		if !b.Carried {
			add(b.Finding)
		}
	}
	for _, f := range append(append([]Finding(nil), a.Optional...), a.Deferred...) {
		add(f)
	}
	o.Output.Annotations = anns
	o.Output.Text = encodeState(stateOf(a, gate))
	return o
}

// stateOf keeps an assessment's blocking findings for the next round. The
// human gate is kept apart: only a person clears it, so no round carries it.
func stateOf(a Assessment, gate *Gate) State {
	st := State{Blocking: []Finding{}, Gate: gate}
	for _, b := range a.Blocking {
		if isGate(b.Finding) || len(st.Blocking) == maxStored {
			continue
		}
		st.Blocking = append(st.Blocking, storable(b.Finding))
	}
	return st
}

func storable(f Finding) Finding {
	f.ID, f.Title = clip(f.ID, maxTitle), clip(f.Title, maxTitle)
	f.Detail = clip(f.Detail, maxStoredDetail)
	f.Defer = false
	return f
}

// takeGate removes the agent's human-gate findings from an assessment;
// they block or clear with the head's human gate, whatever their severity.
func takeGate(a *Assessment) []Finding {
	var out []Finding
	keep := func(bs []Blocked) []Blocked {
		var kept []Blocked
		for _, b := range bs {
			if isGate(b.Finding) {
				out = append(out, b.Finding)
			} else {
				kept = append(kept, b)
			}
		}
		return kept
	}
	a.Blocking, a.Accepted = keep(a.Blocking), keep(a.Accepted)
	findings := func(fs []Finding) []Finding {
		var kept []Finding
		for _, f := range fs {
			if isGate(f) {
				out = append(out, f)
			} else {
				kept = append(kept, f)
			}
		}
		return kept
	}
	a.Optional, a.Deferred = findings(a.Optional), findings(a.Deferred)
	return out
}

func writeFinding(s *strings.Builder, f Finding) {
	fmt.Fprintf(s, "- **%s**", f.Severity)
	if p := cleanPath(f.Path); p != "" {
		if f.Line > 0 {
			fmt.Fprintf(s, " `%s:%d`", p, f.Line)
		} else {
			fmt.Fprintf(s, " `%s`", p)
		}
	}
	fmt.Fprintf(s, " %s (`%s`)", oneLine(f.Title, maxTitle), oneLine(f.ID, maxTitle))
	if f.Detail != "" {
		fmt.Fprintf(s, ": %s", oneLine(f.Detail, maxDetail))
	}
	s.WriteString("\n")
}

// encodeState hides a State in one HTML comment that GitHub does not
// render; base64 keeps the JSON from closing it.
func encodeState(st State) string {
	b, _ := json.Marshal(st)
	return statePrefix + base64.StdEncoding.EncodeToString(b) + stateSuffix
}

// LoadState reads the State a completed review kept in its check run text.
// ok is false when the text holds none, as for a review that ended without
// a report.
func LoadState(text string) (st State, ok bool) {
	m := stateRE.FindStringSubmatch(text)
	if m == nil {
		return State{}, false
	}
	b, err := base64.StdEncoding.DecodeString(m[1])
	if err != nil || json.Unmarshal(b, &st) != nil {
		return State{}, false
	}
	return st, true
}

// NoReport is the completion of a review that produced no usable report:
// it fails the check. The reason is written by the reviewer service.
func NoReport(reason, footer string) Outcome {
	return Outcome{Conclusion: ConclusionFailure, Output: github.CheckRunOutput{
		Title:   "No review report",
		Summary: clip(reason+"\n\nPush a new commit to review again.\n\n"+footer, maxSummary),
	}}
}

// cleanPath is a repository-relative path, or empty when p cannot be one.
func cleanPath(p string) string {
	p = strings.TrimPrefix(strings.TrimSpace(p), "./")
	p = strings.TrimLeft(p, "/")
	if p == "" || len(p) > 1024 || strings.ContainsAny(p, "\n\r\t`") {
		return ""
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == ".." || seg == "." || seg == "" {
			return ""
		}
	}
	return p
}

// untrusted strips the service's markers from text the agent wrote.
func untrusted(s string) string {
	for _, m := range []string{enqueuedMarker, gateOnlyMarker, statePrefix} {
		s = strings.ReplaceAll(s, m, "")
	}
	return s
}

func oneLine(s string, n int) string {
	return clip(strings.Join(strings.Fields(untrusted(s)), " "), n)
}

// ReplyText is the agent's final answer: the task's status message, else
// its last agent message, else its artifacts.
func ReplyText(t *kagent.Task) string {
	if t.Status.Message != nil {
		if s := messageText(t.Status.Message); s != "" {
			return s
		}
	}
	for i := len(t.History) - 1; i >= 0; i-- {
		if m := t.History[i]; m != nil && m.Role != "ROLE_USER" {
			if s := messageText(m); s != "" {
				return s
			}
		}
	}
	var b strings.Builder
	for _, a := range t.Artifacts {
		if a == nil {
			continue
		}
		for _, p := range a.Parts {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

func messageText(m *kagent.Message) string {
	var b strings.Builder
	for _, p := range m.Parts {
		b.WriteString(p.Text)
	}
	return b.String()
}

// plural picks the singular form for one, else the plural (word+"s" when
// no plural is given).
func plural(n int, word string, pl ...string) string {
	if n == 1 {
		return word
	}
	if len(pl) > 0 {
		return pl[0]
	}
	return word + "s"
}

// clip cuts s to at most n bytes on a rune boundary.
func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n] + "…"
}
