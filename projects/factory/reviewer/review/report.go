package review

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"
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
	// Earlier is the agent's statement that a new finding on a later head
	// is on code an earlier review covered and this head left alone (Scope).
	// Left out, the finding keeps its severity.
	Earlier bool `json:"earlier,omitempty"`
	// Lowered is the severity the agent gave a finding Scope lowered; the
	// agent cannot set it.
	Lowered string `json:"-"`
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

// ruleSegments are the parts of a finding ID that may name its rule,
// lowercased: every ':'-separated part after the path, or the whole ID when
// it has no ':'. A rule may carry a symbol suffix (REVIEW.md "Report").
func ruleSegments(id string) []string {
	parts := strings.Split(strings.ToLower(id), ":")
	if len(parts) > 1 {
		parts = parts[1:]
	}
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// ruleName cuts a symbol suffix from a rule segment: the rule is the
// leading run of letters, digits and dashes.
func ruleName(seg string) string {
	if i := strings.IndexFunc(seg, func(r rune) bool {
		return (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '-'
	}); i >= 0 {
		return seg[:i]
	}
	return seg
}

// isGate reports a finding only Joe's approval of the head clears: any rule
// segment of its ID starting with human-gate, whatever symbol follows. It
// matches generously, since a missed gate is a merge without Joe.
func isGate(f Finding) bool {
	for _, s := range ruleSegments(f.ID) {
		if strings.HasPrefix(s, HumanGateID) {
			return true
		}
	}
	return false
}

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
	// Gated are the agent's human-gate findings, at any severity; they block
	// or clear with the head's human gate and never count as low.
	Gated    []Finding
	Blocking []Blocked
	Accepted []Blocked
	Fixed    []Finding
	// Withdrawn are previous findings the agent withdrew as mistaken.
	Withdrawn []Blocked
	// Approved are human-gate findings Approver cleared on this head.
	Approved []Finding
	Approver string
	Optional []Finding
	// Deferred are low findings worth a follow-up, including those Scope
	// lowered, which MaxLow never drops.
	Deferred []Finding
	// DroppedLow counts low findings past MaxLow.
	DroppedLow int
	// Earlier is what earlier heads' reviews cleared; the State carries it
	// on with this review's (clearedOf).
	Earlier []Cleared
}

// withdrawn reports a note withdrawing a previous finding.
func withdrawn(note string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "withdrawn")
}

// MaxLow is how many low findings a check run lists.
const MaxLow = 5

// Assess decides a review: success only when no critical, high or medium
// finding blocks. Human-gate findings are set apart first, whatever their
// severity. A previous blocking finding is resolved when the agent reports
// it fixed, or marks it justified with a note starting "withdrawn" (any
// severity: the finding was wrong); a medium one is also accepted when
// justified with any note. Anything else (open, unaccounted for, a justified
// critical or high, a justification without a note) still blocks. A previous
// finding reported again keeps the higher of its two severities unless it
// is marked fixed or withdrawn. A previous human-gate finding that is not
// fixed or withdrawn is a human-gate finding of this head too: only a
// person's approval of this head clears it.
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
	cleared := func(id string) bool {
		s, ok := status[id]
		return ok && (s.Status == StatusFixed || (s.Status == StatusJustified && withdrawn(s.Note)))
	}
	reason := map[string]string{}
	repeated := func(f Finding) Blocked {
		_, was := priorByID[f.ID]
		return Blocked{Finding: f, Repeated: was, Note: strings.TrimSpace(status[f.ID].Note), Reason: reason[f.ID]}
	}
	current := map[string]bool{}
	low := 0
	for _, f := range r.Findings {
		if isGate(f) {
			current[f.ID] = true
			a.Gated = append(a.Gated, f)
			continue
		}
		current[f.ID] = true
		if p, was := priorByID[f.ID]; was && !cleared(f.ID) && rank(p.Severity) < rank(f.Severity) {
			reason[f.ID] = fmt.Sprintf("reported again as %s; it keeps its earlier severity", f.Severity)
			f.Severity = p.Severity
		}
		switch {
		case f.Severity == SeverityLow && f.Lowered != "":
			a.Deferred = append(a.Deferred, f)
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
		s, ok := status[p.ID]
		note := strings.TrimSpace(s.Note)
		if isGate(p) {
			switch {
			case current[p.ID]:
				// Reported again and set apart above.
			case ok && s.Status == StatusFixed:
				a.Fixed = append(a.Fixed, p)
			case ok && s.Status == StatusJustified && withdrawn(note):
				a.Withdrawn = append(a.Withdrawn, Blocked{Finding: p, Carried: true, Note: note})
			default:
				a.Gated = append(a.Gated, p)
			}
			continue
		}
		if current[p.ID] {
			// Reported again and judged above; a withdrawal is still recorded.
			if ok && s.Status == StatusJustified && withdrawn(note) {
				a.Withdrawn = append(a.Withdrawn, Blocked{Finding: p, Note: note})
			}
			continue
		}
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

// Scope is what a later head changed since the previous reviewed head. A new
// finding on code that review covered and this head left alone is low with
// defer unless it is critical (REVIEW.md "Later heads"); Scope enforces that
// for the high and medium findings it can place.
type Scope struct {
	paths map[string]bool
}

// LaterScope is the scope of a head reviewed after since, or nil when there
// is none to enforce: a first review, or an interdiff that is missing,
// truncated or spans a rebase.
func LaterScope(since string, d *github.CommitComparison) *Scope {
	if since == "" || d == nil || d.Status != "ahead" || d.Truncated {
		return nil
	}
	s := &Scope{paths: map[string]bool{}}
	for _, f := range d.Files {
		s.paths[f.Path] = true
		if f.PreviousPath != "" {
			s.paths[f.PreviousPath] = true
		}
	}
	return s
}

// changed reports a path that names a file changed since the previous head.
func (s *Scope) changed(p string) bool {
	p = strings.TrimSpace(p)
	return p != "" && (s.paths[p] || s.paths[cleanPath(p)])
}

// Lower returns r with each new high or medium finding lowered to a
// deferred low one when the agent marks it earlier (on code an earlier
// review covered and this head left alone) and its file is outside the files
// changed since the previous head. Unmarked findings, critical findings,
// human-gate findings, the previous round's IDs, findings with no path and
// findings about the review itself (processRules) keep their severity, so an
// omitted mark never lowers a regression. A nil Scope lowers nothing.
func (s *Scope) Lower(r *Report, prior []Finding) *Report {
	if s == nil {
		return r
	}
	known := map[string]bool{}
	for _, p := range prior {
		known[p.ID] = true
	}
	out := *r
	out.Findings = slices.Clone(r.Findings)
	for i := range out.Findings {
		f := &out.Findings[i]
		if (f.Severity != SeverityHigh && f.Severity != SeverityMedium) || isGate(*f) || known[f.ID] ||
			!f.Earlier || strings.TrimSpace(f.Path) == "" || isProcess(*f) || s.changed(f.Path) {
			continue
		}
		f.Lowered, f.Severity, f.Defer = f.Severity, SeverityLow, true
	}
	return &out
}

// processRules name findings about the review itself, not about a place in
// previously reviewed code: an unread or unverifiable change, injected
// instructions, or a weakened gate. Lower never lowers them.
var processRules = []string{"unreviewed", "unverified", "review-injection", "gate"}

func isProcess(f Finding) bool {
	for _, s := range ruleSegments(f.ID) {
		if slices.Contains(processRules, ruleName(s)) {
			return true
		}
	}
	return false
}

func sortBlocked(bs []Blocked) {
	// Stable insertion sort by severity keeps the agent's order within one.
	for i := 1; i < len(bs); i++ {
		for j := i; j > 0 && rank(bs[j].Severity) < rank(bs[j-1].Severity); j-- {
			bs[j], bs[j-1] = bs[j-1], bs[j]
		}
	}
}

// Outcome is how a review's check run completes, and what the PR comment
// shows for it; a nil Comment leaves the comment as it is.
type Outcome struct {
	Conclusion string
	Output     github.CheckRunOutput
	Comment    *CommentUpdate
}

// Output limits. GitHub takes 65535 characters in a summary or text.
const (
	maxSummary      = 60000
	maxAgentSummary = 4000
	// maxPreface is the room Render leaves for a note the engine puts before
	// the summary, and maxOmitted for the line counting blocking findings
	// that did not fit.
	maxPreface      = 1000
	maxOmitted      = 200
	maxTitle        = 300
	maxDetail       = 1500
	maxMessage      = 4000
	maxStored       = 40
	maxStoredDetail = 500
	maxStoredPath   = 300
	// maxState bounds the encoded State, well under GitHub's limit.
	maxState = 48000
)

// enqueuedMarker records on a review check run that its head was enqueued
// once. The state marker holds a completed review's State for the next
// round, and the task marker an in-progress review's attempt and task.
// gateOnlyMarker marks a check whose only blocking finding is the human
// gate, which no revision round can fix. Every marker starts with
// markerStart, which is stripped from anything the agent or the PR wrote.
const (
	markerStart         = "<!-- factory-review:"
	enqueuedMarker      = markerStart + "enqueued -->"
	gateOnlyMarker      = markerStart + "human-gate-only -->"
	duplicateHeadMarker = markerStart + "duplicate-head -->"
	labelHistoryMarker  = markerStart + "label-history-unavailable -->"
	statePrefix         = markerStart + "state v1 "
	stateSuffix         = " -->"
)

var stateRE = regexp.MustCompile(`<!-- factory-review:state v1 ([A-Za-z0-9+/=]*) -->`)

// State keeps a running review's label snapshot, then its blocking
// findings and human gate on the completed check. The next round must
// account for the findings; only a person clears the human gate.
type State struct {
	Blocking []Finding `json:"blocking"`
	Cleared  []Cleared `json:"cleared,omitempty"`
	Gate     *Gate     `json:"gate,omitempty"`
}

// Cleared is a finding a review of an earlier head cleared: fixed,
// justified or withdrawn, with the agent's note. Each head's review runs in
// a fresh session, so the prompt lists these to keep it from raising one
// again on code the head left alone.
type Cleared struct {
	ID     string `json:"id"`
	Status string `json:"status"`
	Title  string `json:"title,omitempty"`
	Note   string `json:"note,omitempty"`
}

// StatusWithdrawn marks a cleared finding the agent withdrew as mistaken.
const StatusWithdrawn = "withdrawn"

// Bounds on the cleared record.
const (
	maxCleared      = 20
	maxClearedTitle = 200
	maxClearedNote  = 300
)

// Previous is what the next head's review accounts for: the blocking
// findings and the agent's human-gate findings. The service's own gate, by
// path or label, is found again on every head.
func (st State) Previous() []Finding {
	out := append([]Finding(nil), st.Blocking...)
	if st.Gate != nil {
		out = append(out, st.Gate.Findings...)
	}
	return out
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

	// The summary is the overview, the blocking findings, the other sections
	// and a tail of the human-hold marker and the footer. The connector reads
	// the marker and the findings, so the tail is never clipped and the
	// blocking findings get the space before the other sections.
	tail := footer
	if gate != nil && gate.Authorship != "" {
		tail = gate.Authorship + "\n\n" + tail
	}
	if len(a.Blocking) > 0 && !slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return !isGate(b.Finding) }) {
		tail = gateOnlyMarker + footer
	}
	budget := maxSummary - maxPreface - len(tail)
	var out strings.Builder
	if sum := strings.TrimSpace(untrusted(r.Summary)); sum != "" {
		out.WriteString(clip(sum, maxAgentSummary))
		out.WriteString("\n\n")
	}
	if len(a.Blocking) > 0 {
		out.WriteString("### Blocking\n\n")
		listed := 0
		for _, e := range blockingEntries(a.Blocking, budget-out.Len()-maxOmitted) {
			if out.Len()+len(e)+maxOmitted > budget {
				break
			}
			out.WriteString(e)
			listed++
		}
		if n := len(a.Blocking) - listed; n > 0 {
			fmt.Fprintf(&out, "\n%d more blocking %s did not fit in this check. Fix those listed and push; the next review lists the rest.\n",
				n, plural(n, "finding"))
		}
		out.WriteString("\n")
	}
	var s strings.Builder
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
			if f.Lowered != "" {
				fmt.Fprintf(&s, "  %s\n", loweredNote(f))
			}
		}
		s.WriteString("\n")
	}
	if len(a.Blocking)+len(a.Accepted)+len(a.Withdrawn)+len(a.Fixed)+len(a.Approved)+optional == 0 {
		s.WriteString("No findings.\n\n")
	}
	if room := budget - out.Len(); room > 0 {
		out.WriteString(clip(s.String(), room))
	}
	out.WriteString(tail)
	o.Output.Summary = out.String()

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
	o.Output.Text = encodeState(stateOf(a, r, gate))
	return o
}

// loweredNote says why Scope lowered a finding.
func loweredNote(f Finding) string {
	return fmt.Sprintf("Reported %s; lowered because the reviewer placed it on code an earlier review covered, outside the files changed since the previous review (REVIEW.md \"Later heads\").",
		oneLine(f.Lowered, 20))
}

// stateOf keeps an assessment's blocking findings for the next round, and
// what this and earlier reviews cleared. The human gate is kept apart: only
// a person clears it, so no round carries it.
func stateOf(a Assessment, r *Report, gate *Gate) State {
	st := State{Blocking: []Finding{}, Cleared: clearedOf(a, r), Gate: gate}
	for _, b := range a.Blocking {
		if isGate(b.Finding) || len(st.Blocking) == maxStored {
			continue
		}
		st.Blocking = append(st.Blocking, storable(b.Finding))
	}
	return st
}

// clearedOf is the cleared record for the next head: this review's fixed,
// justified and withdrawn findings, then the earlier ones, at most
// maxCleared. A finding that blocks this head is not cleared, and
// human-gate findings are left out: only a person clears the gate.
func clearedOf(a Assessment, r *Report) []Cleared {
	notes := map[string]string{}
	if r != nil {
		for _, p := range r.Previous {
			if _, ok := notes[p.ID]; !ok {
				notes[p.ID] = p.Note
			}
		}
	}
	skip := map[string]bool{}
	for _, b := range a.Blocking {
		skip[b.ID] = true
	}
	var out []Cleared
	add := func(f Finding, status, note string) {
		id := storeText(f.ID, maxTitle)
		if isGate(f) || skip[f.ID] || skip[id] || len(out) == maxCleared {
			return
		}
		skip[id] = true
		out = append(out, Cleared{ID: id, Status: status, Title: storeText(f.Title, maxClearedTitle), Note: storeText(note, maxClearedNote)})
	}
	for _, f := range a.Fixed {
		add(f, StatusFixed, notes[f.ID])
	}
	for _, b := range a.Accepted {
		add(b.Finding, StatusJustified, b.Note)
	}
	for _, b := range a.Withdrawn {
		add(b.Finding, StatusWithdrawn, b.Note)
	}
	for _, c := range a.Earlier {
		add(Finding{ID: c.ID, Title: c.Title}, c.Status, c.Note)
	}
	return out
}

// storable bounds a finding for the hidden State: every field is one line
// of printable text, clipped.
func storable(f Finding) Finding {
	f.ID, f.Title = storeText(f.ID, maxTitle), storeText(f.Title, maxTitle)
	f.Path = storeText(f.Path, maxStoredPath)
	f.Detail = storeText(f.Detail, maxStoredDetail)
	f.Defer, f.Earlier = false, false
	return f
}

func storeText(s string, n int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || r == utf8.RuneError {
			return ' '
		}
		return r
	}, s)
	return oneLine(s, n)
}

// blockingEntries renders the blocking findings in full when they all fit in
// room. Otherwise it renders every one with its detail and note cut to a
// shared length, down to none, so a revision gets each finding's location
// and title before any finding is left out.
func blockingEntries(bs []Blocked, room int) []string {
	var entries []string
	for _, limit := range []int{maxMessage, 500, 200, 0} {
		entries = entries[:0]
		total := 0
		for _, b := range bs {
			e := blockingEntry(b, limit)
			entries = append(entries, e)
			total += len(e)
		}
		if total <= room {
			break
		}
	}
	return entries
}

// blockingEntry renders one blocking finding with its detail and note cut to
// limit characters (the detail to at most maxDetail); 0 leaves both out.
func blockingEntry(b Blocked, limit int) string {
	var e strings.Builder
	writeFindingN(&e, b.Finding, min(limit, maxDetail))
	switch {
	case b.Carried:
		fmt.Fprintf(&e, "  From the previous review: %s.", b.Reason)
	case b.Repeated && b.Reason != "":
		fmt.Fprintf(&e, "  Raised in the previous review and still present; %s.", b.Reason)
	case b.Repeated:
		e.WriteString("  Raised in the previous review and still present.")
	}
	if b.Note != "" && limit > 0 {
		fmt.Fprintf(&e, " Note: %s", oneLine(b.Note, min(limit, maxMessage)))
	}
	e.WriteString("\n")
	return e.String()
}

func writeFinding(s *strings.Builder, f Finding) {
	writeFindingN(s, f, maxDetail)
}

// writeFindingN writes a finding as one list line, its detail cut to
// detail characters; 0 leaves the detail out.
func writeFindingN(s *strings.Builder, f Finding, detail int) {
	fmt.Fprintf(s, "- **%s**", f.Severity)
	if p := cleanPath(f.Path); p != "" {
		if f.Line > 0 {
			fmt.Fprintf(s, " `%s:%d`", p, f.Line)
		} else {
			fmt.Fprintf(s, " `%s`", p)
		}
	}
	fmt.Fprintf(s, " %s (`%s`)", oneLine(f.Title, maxTitle), oneLine(f.ID, maxTitle))
	if f.Detail != "" && detail > 0 {
		fmt.Fprintf(s, ": %s", oneLine(f.Detail, detail))
	}
	s.WriteString("\n")
}

// encodeState hides a State in one HTML comment that GitHub does not
// render; base64 keeps the JSON from closing it. Past maxState it shortens
// the cleared record to IDs and statuses, then drops it, and never drops
// the blocking findings or the gate: their titles and details go first,
// then paths, until only IDs and severities are left.
func encodeState(st State) string {
	enc := func(st State) string {
		var b strings.Builder
		e := json.NewEncoder(&b)
		e.SetEscapeHTML(false)
		_ = e.Encode(st)
		return statePrefix + base64.StdEncoding.EncodeToString([]byte(strings.TrimSpace(b.String()))) + stateSuffix
	}
	out := enc(st)
	if len(out) > maxState && len(st.Cleared) > 0 {
		bare := make([]Cleared, len(st.Cleared))
		for i, c := range st.Cleared {
			bare[i] = Cleared{ID: c.ID, Status: c.Status}
		}
		st.Cleared = bare
		out = enc(st)
	}
	if len(out) > maxState && len(st.Cleared) > 0 {
		st.Cleared = nil // Drop the record whole before any blocking text.
		out = enc(st)
	}
	for _, lim := range []struct{ text, detail, path, id int }{
		{120, 120, maxStoredPath, maxTitle}, {0, 0, 120, 120}, {0, 0, 0, 80},
	} {
		if len(out) <= maxState {
			break
		}
		cut := func(s string, n int) string {
			if n == 0 {
				return ""
			}
			return clip(s, n)
		}
		shrink := func(fs []Finding) []Finding {
			out := make([]Finding, len(fs))
			for i, f := range fs {
				f.Title, f.Detail = cut(f.Title, lim.text), cut(f.Detail, lim.detail)
				f.Path, f.ID = cut(f.Path, lim.path), clip(f.ID, lim.id)
				if f.Path == "" {
					f.Line = 0
				}
				out[i] = f
			}
			return out
		}
		small := State{Blocking: shrink(st.Blocking)} // Without the cleared record.
		if g := st.Gate; g != nil {
			gate := *g // Keep approval freshness and label observations through trimming.
			gate.Paths, gate.Findings = nil, shrink(g.Findings)
			small.Gate = &gate
			for _, p := range g.Paths {
				small.Gate.Paths = append(small.Gate.Paths, clip(p, max(lim.path, 80)))
			}
		}
		out = enc(small)
	}
	return out
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

// NoReport is the completion of a review that produced no usable report in
// any of its attempts: it fails the check. The reason is written by the
// reviewer service.
func NoReport(reason, footer string) Outcome {
	return Outcome{Conclusion: ConclusionFailure, Output: github.CheckRunOutput{
		Title: "No review report",
		Summary: clip(untrusted(reason)+fmt.Sprintf("\n\nThe reviewer gave up after %d attempts; push a new commit to review again.\n\n", MaxAttempts)+
			footer, maxSummary),
	}, Comment: &CommentUpdate{Verdict: VerdictFailed, Headline: "No review report", Note: reason + " Push a new commit to review again."}}
}

// lookupPath is p as a path that rules and gates are looked up for. Any
// character counts; only an empty or ".." segment makes it unusable.
func lookupPath(p string) (string, bool) {
	if p == "" {
		return "", false
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == "" || seg == ".." {
			return "", false
		}
	}
	return p, true
}

// quotePath writes a path from the PR or the agent as a quoted string: no
// newline, backtick or service marker survives in it.
func quotePath(p string) string {
	return untrusted(strings.ReplaceAll(strconv.Quote(p), "`", `\x60`))
}

// cleanPath is a repository-relative path fit for an annotation or a code
// span, without service markers, or empty when p cannot be one.
func cleanPath(p string) string {
	p = strings.TrimPrefix(strings.TrimSpace(untrusted(p)), "./")
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

// untrusted strips the start of every service marker from text the agent or
// the PR wrote, until none is left: removing one cannot form another.
func untrusted(s string) string {
	for strings.Contains(s, markerStart) {
		s = strings.ReplaceAll(s, markerStart, "")
	}
	return s
}

// oneLine collapses s to one line of single spaces, then strips service
// markers, so whitespace inside a marker cannot hide it.
func oneLine(s string, n int) string {
	return clip(untrusted(strings.Join(strings.Fields(s), " ")), n)
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
