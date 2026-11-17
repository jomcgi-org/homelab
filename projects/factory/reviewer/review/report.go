package review

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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
	// Verdict is the goal-check verdict the service rendered a goal finding
	// from (goalFindings); the agent cannot set it. A goal finding reported
	// again as after_rollout takes that verdict's low severity: like met, it
	// says the diff makes the change it needs (HOME-179). Other verdicts keep
	// the re-report rule, so an unclear answer cannot lower a not_met to a
	// severity a note can justify.
	Verdict string `json:"-"`
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
// the path and title; an unknown previous status counts as open. An error
// names a fixed class and never a value from the reply, since the check
// publishes it.
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
		return nil, errors.New("the last fenced block is not JSON")
	}
	dec := json.NewDecoder(strings.NewReader(last[2]))
	var r Report
	if err := dec.Decode(&r); err != nil {
		return nil, errors.New("the last fenced block is not a findings report")
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
			return nil, fmt.Errorf("finding %d has a severity that is not one of %s", i+1, strings.Join(Severities, ", "))
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
	// Decision is Joe's comment that justified an accepted high finding.
	Decision *DecisionStamp
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
	// Carried are the previous head's decisions that still hold.
	Carried []DecisionStamp
	// EarlierPartial carries an incomplete cleared record forward.
	EarlierPartial bool
}

// withdrawn reports a note withdrawing a previous finding.
func withdrawn(note string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "withdrawn")
}

// Decisions are Joe's comments on one PR: the trusted comments by a
// `humanApprovers` login, which are Joe's accounts (REVIEW.md "Joe
// decides"). Only they can justify a high finding (REVIEW.md "Clearing a
// finding without a fix").
type Decisions struct {
	Repo     string
	Number   int
	Comments []github.Comment
	// Carried are the decisions the previous head's pass relied on
	// (State.Decisions). Each stays while its comment is unchanged; a
	// changed one restores its finding (AssessDecided).
	Carried []DecisionStamp
}

// DecisionStamp is Joe's comment that justified high finding Finding, with
// a hash of its text then: an edit or a deletion revokes it.
type DecisionStamp struct {
	Finding string `json:"finding,omitempty"`
	ID      int64  `json:"id"`
	Hash    string `json:"hash"`
}

func bodyHash(body string) string {
	sum := sha256.Sum256([]byte(body))
	return hex.EncodeToString(sum[:])
}

// decided is Joe's comment that note cites to justify finding id, or nil.
// The note starts "decision:" and links the comment on this PR
// (https://github.com/<repo>/pull/<n>#issuecomment-<id>, or /issues/), and
// the comment's text names id. The agent's note alone grants nothing.
func (d Decisions) decided(note, id string) *DecisionStamp {
	if d.Repo == "" || d.Number <= 0 || strings.TrimSpace(id) == "" ||
		!strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "decision:") {
		return nil
	}
	link := regexp.MustCompile(`(?i)https://github\.com/` + regexp.QuoteMeta(d.Repo) + `/(?:pull|issues)/` +
		strconv.Itoa(d.Number) + `#issuecomment-([1-9][0-9]*)\b`)
	for _, m := range link.FindAllStringSubmatch(note, -1) {
		cid, err := strconv.ParseInt(m[1], 10, 64)
		if err != nil {
			continue
		}
		for _, c := range d.Comments {
			if c.ID == cid && namesID(c.Body, id) {
				return &DecisionStamp{Finding: clip(id, maxTitle), ID: cid, Hash: bodyHash(c.Body)}
			}
		}
	}
	return nil
}

// holds reports that Joe's comment still exists with the text it had.
func (d Decisions) holds(st DecisionStamp) bool {
	for _, c := range d.Comments {
		if st.ID > 0 && c.ID == st.ID && st.Hash != "" && bodyHash(c.Body) == st.Hash {
			return true
		}
	}
	return false
}

// Hold reports that every decision a review relied on still holds.
func (d Decisions) Hold(stamps []DecisionStamp) bool {
	for _, st := range stamps {
		if !d.holds(st) {
			return false
		}
	}
	return true
}

// Revalidate keeps each cleared finding's decision only while Joe's comment
// is unchanged: an edited or deleted comment grants nothing.
func (d Decisions) Revalidate(cleared []Cleared) []Cleared {
	out := slices.Clone(cleared)
	for i := range out {
		if out[i].Decision != nil && !d.holds(*out[i].Decision) {
			out[i].Decision = nil
		}
	}
	return out
}

// namesID reports text that names the finding id as a whole word: the
// characters around it cannot continue an ID, so `a.go:x` does not name
// `a.go:x-y`. A full stop after it ends a sentence.
func namesID(text, id string) bool {
	text, id = strings.ToLower(text), strings.ToLower(strings.TrimSpace(id))
	idChar := func(r byte) bool {
		return r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || strings.IndexByte("-_:#/", r) >= 0
	}
	for from := 0; from <= len(text)-len(id); {
		i := strings.Index(text[from:], id)
		if i < 0 {
			return false
		}
		start, end := from+i, from+i+len(id)
		before := start == 0 || !(idChar(text[start-1]) || text[start-1] == '.')
		after := end == len(text) || !idChar(text[end]) && (text[end] != '.' || end+1 == len(text) || !idChar(text[end+1]))
		if before && after {
			return true
		}
		from = start + 1
	}
	return false
}

// decisions are Joe's comments that this head's pass relies on, once each:
// those its accepted findings cite, those that keep earlier heads' findings
// cleared (Earlier, already revalidated), and the previous head's that still
// hold (Carried), which the bounded cleared record may have lost.
func (a Assessment) decisions() []DecisionStamp {
	var out []DecisionStamp
	add := func(d *DecisionStamp) {
		if d != nil && !slices.Contains(out, *d) {
			out = append(out, *d)
		}
	}
	for _, b := range a.Accepted {
		add(b.Decision)
	}
	for _, c := range a.Earlier {
		add(c.Decision)
	}
	for i := range a.Carried {
		add(&a.Carried[i])
	}
	return out
}

// MaxLow is how many low findings a check run lists.
const MaxLow = 5

// Assess decides a review: success only when no critical, high or medium
// finding blocks. Human-gate findings are set apart first, whatever their
// severity. A previous blocking finding is resolved when the agent reports
// it fixed, or marks it justified with a note starting "withdrawn" (any
// severity: the finding was wrong). One whose severity on this head is
// medium is also accepted when justified with any note, and one whose
// severity is high when the note cites Joe's comment that names it
// (Decisions.decided). Anything else (open, unaccounted for, a justified
// critical, a justified high without such a decision, a justification
// without a note) still blocks. A previous
// finding reported again keeps the higher of its two severities unless it
// is marked fixed or withdrawn. A previous human-gate finding that is not
// fixed or withdrawn is a human-gate finding of this head too: only a
// person's approval of this head clears it.
func Assess(r *Report, prior []Finding) Assessment {
	return AssessDecided(r, prior, Decisions{})
}

// AssessDecided is Assess with Joe's comments on the PR, which can justify a
// high finding.
func AssessDecided(r *Report, prior []Finding, d Decisions) Assessment {
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
	// justified judges a justification by the finding's severity on this
	// head, so a medium one raised to high needs Joe's decision.
	justified := func(id, severity string) (string, *DecisionStamp, bool) {
		p, wasPrior := priorByID[id]
		s, ok := status[id]
		note := strings.TrimSpace(s.Note)
		if !wasPrior || !ok || s.Status != StatusJustified || note == "" || isGate(p) {
			return note, nil, false
		}
		switch severity {
		case SeverityMedium:
			return note, nil, true
		case SeverityHigh:
			st := d.decided(note, id)
			return note, st, st != nil
		}
		return note, nil, false
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
	// The previous head's decisions: one that holds stays relied on, even
	// when the bounded cleared record lost its finding. One whose comment
	// changed is revoked, and its finding is high again.
	revoked := map[string]bool{}
	for _, st := range d.Carried {
		switch {
		case d.holds(st):
			a.Carried = append(a.Carried, st)
		case st.Finding != "":
			revoked[st.Finding] = true
		}
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
		if p, was := priorByID[f.ID]; was && f.Verdict != goalLive && !cleared(f.ID) && rank(p.Severity) < rank(f.Severity) {
			reason[f.ID] = fmt.Sprintf("reported again as %s; it keeps its earlier severity", f.Severity)
			f.Severity = p.Severity
		}
		if revoked[f.ID] && rank(f.Severity) > rank(SeverityHigh) {
			reason[f.ID] = fmt.Sprintf("reported again as %s after Joe's decision changed; it stays high", f.Severity)
			f.Severity, f.Lowered = SeverityHigh, ""
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
		case f.Severity == SeverityMedium || f.Severity == SeverityHigh:
			if note, cid, ok := justified(f.ID, f.Severity); ok {
				a.Accepted = append(a.Accepted, Blocked{Finding: f, Repeated: true, Note: note, Decision: cid})
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
		if _, cid, accepted := justified(p.ID, p.Severity); accepted {
			a.Accepted = append(a.Accepted, Blocked{Finding: p, Carried: true, Note: note, Decision: cid})
			continue
		}
		b := Blocked{Finding: p, Carried: true, Note: note}
		switch {
		case !ok:
			b.Reason = "not accounted for"
		case s.Status == StatusFixed:
			a.Fixed = append(a.Fixed, p)
			continue
		case s.Status == StatusJustified && p.Severity == SeverityHigh:
			b.Reason = "justified, but a high finding needs a note starting \"decision:\" that links Joe's comment naming it"
		case s.Status == StatusJustified && p.Severity != SeverityMedium:
			b.Reason = "justified, but only a medium or a decided high finding can be justified"
		case s.Status == StatusJustified:
			b.Reason = "justified without a note"
		default:
			b.Reason = "still open"
		}
		a.Blocking = append(a.Blocking, b)
	}
	// A revoked decision restores its finding as a blocking high, unless
	// this review reports it (high at least, above) or accounts for it.
	for _, st := range d.Carried {
		if !revoked[st.Finding] || current[st.Finding] {
			continue
		}
		if _, was := priorByID[st.Finding]; was {
			continue
		}
		revoked[st.Finding] = false // Once, for several stamps of one finding.
		a.Blocking = append(a.Blocking, Blocked{
			Finding: Finding{
				ID: st.Finding, Severity: SeverityHigh, Title: "Joe's decision on this finding was edited or deleted",
				Detail: "An earlier head passed on Joe's decision for this finding. Its comment has changed, so the finding blocks until it is fixed or decided again.",
			},
			Carried: true, Reason: "Joe's decision changed",
		})
	}
	sortBlocked(a.Blocking)
	a.Conclusion = ConclusionSuccess
	if len(a.Blocking) > 0 {
		a.Conclusion = ConclusionFailure
	}
	return a
}

// Scope is what a later head changed since the previous reviewed head. A new
// finding on code that review covered and this head left alone is high,
// critical or low with defer (REVIEW.md "Later heads"); Scope enforces that
// for the medium findings it can place.
type Scope struct {
	paths map[string]bool
}

// LaterScope is the scope of a head reviewed after since, or nil when there
// is none to enforce: a first review, or an interdiff that is missing,
// truncated or spans a rebase.
func LaterScope(since string, d *github.CommitComparison) *Scope {
	// An answer with no file list at all, unlike an empty one, shows nothing.
	if since == "" || d == nil || d.Status != "ahead" || d.Truncated || d.Files == nil {
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

// Lower returns r with each new medium finding lowered to a deferred low
// one when the agent marks it earlier (on code an earlier
// review covered and this head left alone) and its file is outside the files
// changed since the previous head. Unmarked findings, high and critical
// findings, human-gate findings, the previous round's IDs, IDs an earlier
// review recorded as fixed, findings with no path and findings under any rule
// but lowerableRules keep their severity, so neither an omitted mark nor a
// regression missed on an intervening head lowers one. A nil Scope lowers
// nothing.
func (s *Scope) Lower(r *Report, prior []Finding, cleared []Cleared) *Report {
	if s == nil {
		return r
	}
	known := map[string]bool{}
	for _, p := range prior {
		known[p.ID] = true
	}
	for _, c := range cleared {
		if c.Status == StatusFixed {
			known[c.ID] = true
		}
	}
	out := *r
	out.Findings = slices.Clone(r.Findings)
	for i := range out.Findings {
		f := &out.Findings[i]
		if f.Severity != SeverityMedium || isGate(*f) || known[f.ID] ||
			!f.Earlier || strings.TrimSpace(f.Path) == "" || !lowerable(*f) || s.changed(f.Path) {
			continue
		}
		f.Lowered, f.Severity, f.Defer = f.Severity, SeverityLow, true
	}
	return &out
}

// lowerableRules are the root contract rules whose late findings Lower may
// lower. Any other rule keeps its severity: a scoped rule sets its own, and
// unverified, unreviewed, review-injection, gate and human-gate findings are
// about the review or its gate, not a place in earlier-reviewed code.
var lowerableRules = []string{"correctness", "security", "boundary", "reachability", "validation", "rollout", "goal"}

// lowerable reports a finding whose rule, the first segment after its path,
// is in lowerableRules.
func lowerable(f Finding) bool {
	segs := ruleSegments(f.ID)
	return len(segs) > 0 && slices.Contains(lowerableRules, ruleName(segs[0]))
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
	Semgrep  *SemgrepStamp `json:"semgrep,omitempty"`
	Blocking []Finding     `json:"blocking"`
	Cleared  []Cleared     `json:"cleared,omitempty"`
	// ClearedPartial marks a cleared record that has lost entries, on this
	// head or an earlier one, so it cannot show every fixed finding.
	ClearedPartial bool  `json:"cleared_partial,omitempty"`
	Gate           *Gate `json:"gate,omitempty"`

	// Session is the kagent session that reviewed the head, which the next
	// head's review may resume (session.go).
	Session *SessionRef `json:"session,omitempty"`

	// Decisions are Joe's comments this head's review relied on to accept
	// a high finding. Every poll checks they still hold (engine.go).
	Decisions []DecisionStamp `json:"decisions,omitempty"`

	// IssueDigest binds the report to the external evidence it reviewed.
	// IssuesPending revokes that report until a fresh review can run.
	IssueDigest   string `json:"issueDigest,omitempty"`
	IssuesPending bool   `json:"issuesPending,omitempty"`
	// Goal caches the goal check for this head and IssueDigest's evidence.
	Goal *GoalCheck `json:"goal,omitempty"`
}

// ClearedComplete reports whether the cleared record still holds every
// finding earlier reviews cleared. A record at its bound from before
// ClearedPartial existed may have lost some.
func (st State) ClearedComplete() bool {
	return !st.ClearedPartial && len(st.Cleared) < maxCleared
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
	// Decision is Joe's comment that justified a high finding, set only
	// by Assess; Decisions.Revalidate clears it when the comment changes.
	Decision *DecisionStamp `json:"decision,omitempty"`
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
	cleared, partial := clearedOf(a, r)
	st := State{Blocking: []Finding{}, Cleared: cleared, ClearedPartial: partial || a.EarlierPartial, Gate: gate, Decisions: a.decisions()}
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
func clearedOf(a Assessment, r *Report) ([]Cleared, bool) {
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
	partial := false
	add := func(f Finding, status, note string, decision *DecisionStamp) {
		id := storeText(f.ID, maxTitle)
		if isGate(f) || skip[f.ID] || skip[id] {
			return
		}
		if len(out) == maxCleared {
			partial = true
			return
		}
		skip[id] = true
		out = append(out, Cleared{ID: id, Status: status, Title: storeText(f.Title, maxClearedTitle), Note: storeText(note, maxClearedNote), Decision: decision})
	}
	for _, f := range a.Fixed {
		add(f, StatusFixed, notes[f.ID], nil)
	}
	for _, b := range a.Accepted {
		add(b.Finding, StatusJustified, b.Note, b.Decision)
	}
	for _, b := range a.Withdrawn {
		add(b.Finding, StatusWithdrawn, b.Note, nil)
	}
	for _, c := range a.Earlier {
		add(Finding{ID: c.ID, Title: c.Title}, c.Status, c.Note, c.Decision)
	}
	return out, partial
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
		st.Cleared, st.ClearedPartial = nil, true // Drop the record whole before any blocking text.
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
		// Without the cleared record, so it says the record is partial.
		small := State{
			Blocking: shrink(st.Blocking), ClearedPartial: st.ClearedPartial || len(st.Cleared) > 0, Session: st.Session, Decisions: st.Decisions,
			IssueDigest: st.IssueDigest, IssuesPending: st.IssuesPending, Goal: st.Goal, Semgrep: st.Semgrep,
		}
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
