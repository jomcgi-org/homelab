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

type InvariantScopeAnswer struct {
	Path      string `json:"path"`
	Uncovered bool   `json:"uncovered"`
	Reason    string `json:"reason"`
}

// Report is the reviewer agent's findings and optional activation assessment.
// The service decides the conclusion; any verdict the agent declares is ignored.
type Report struct {
	InvariantScopes []InvariantScopeAnswer `json:"-"`
	Summary         string                 `json:"summary"`
	// Activation is independently assessed text, never an instruction copied
	// blindly from the commit. ActivationHead is set only by the service.
	Activation     string     `json:"activation,omitempty"`
	ActivationHead string     `json:"-"`
	Findings       []Finding  `json:"findings"`
	Previous       []Previous `json:"previous"`
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
	// Cause is the agent's account of a new blocking finding on a later
	// head: what changed since the previous reviewed head, and how that
	// change introduced or exposed the failure (REVIEW.md "Later heads").
	// Scope checks that it names a changed path.
	Cause string `json:"cause,omitempty"`
	// Kind is what clears a service finding: KindEvidence or KindHold. The
	// agent cannot set it (ParseReport); findingKind derives the rest.
	Kind string `json:"kind,omitempty"`
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

// What a blocking finding asks for. A defect needs a code change; missing
// evidence needs the named test, result or document, and a code change only
// if that evidence shows a defect; a hold clears when a person or an outside
// service acts, and no code change clears it.
const (
	KindDefect   = "defect"
	KindEvidence = "evidence"
	KindHold     = "hold"
)

// findingKind is what clears f: the service's own Kind, a hold for the
// human gate, missing evidence for the unverified and unreviewed rules and
// an unclear goal verdict, and a defect otherwise.
func findingKind(f Finding) string {
	if f.Kind == KindEvidence || f.Kind == KindHold {
		return f.Kind
	}
	if isGate(f) {
		return KindHold
	}
	for _, seg := range ruleSegments(f.ID) {
		if r := ruleName(seg); r == "unverified" || r == "unreviewed" {
			return KindEvidence
		}
	}
	if f.Verdict == goalUnclear {
		return KindEvidence
	}
	return KindDefect
}

// kindActions say what each kind of blocking finding asks of the author.
var kindActions = map[string]string{
	KindDefect: "**Defect**: change the code so that the failure no longer occurs.",
	KindEvidence: "**Missing evidence**: this is not a confirmed defect. Add or point to the evidence the finding names " +
		"(a test CI runs, a CI result, a document the reviewer can read). Change code only if that evidence shows a defect.",
	KindHold: "**Hold**: no code change clears it. It clears when the named person or outside service acts; " +
		"the reviewer service then reviews the head again.",
}

// ParseReport reads the last fenced block of the agent's reply as a report.
// No block, a block in another language, invalid JSON, a report without a
// summary or a findings list, an unknown severity or a finding without a
// title or detail is an error, never a pass. A missing ID is derived from
// the path and title; invalid or duplicate IDs are errors. An unknown
// previous status counts as open.
// Errors name fixed classes, never reply values, since checks publish them.
func ParseReport(reply string) (*Report, error) {
	return parseReport(reply, false)
}

// Historical reports must use the identity the service gave them at the
// time. New reports always use collision-resistant derivation. Ambiguous
// legacy identities still fail the duplicate validation below.
func parseReport(reply string, legacy bool) (*Report, error) {
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
		Findings        json.RawMessage `json:"findings"`
		InvariantScopes json.RawMessage `json:"invariant_scopes"`
	}
	if json.Unmarshal([]byte(last[2]), &probe) != nil || probe.Findings == nil || string(probe.Findings) == "null" {
		return nil, errors.New("the report has no findings list")
	}
	// Advisory data cannot invalidate the required findings report.
	var answers []json.RawMessage
	if json.Unmarshal(probe.InvariantScopes, &answers) == nil {
		for _, raw := range answers {
			var answer InvariantScopeAnswer
			if json.Unmarshal(raw, &answer) == nil && answer.Path != "" {
				r.InvariantScopes = append(r.InvariantScopes, answer)
			}
		}
	}
	if strings.TrimSpace(r.Summary) == "" {
		return nil, errors.New("the report has no summary")
	}
	ids := map[string]bool{}
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
		if legacy {
			f.ID = strings.TrimSpace(f.ID)
		}
		if f.ID == "" {
			f.ID = derivedID(f)
			if legacy {
				slug := strings.Trim(slugJunk.ReplaceAllString(strings.ToLower(f.Title), "-"), "-")
				f.ID = clip(strings.ToLower(f.Path)+":"+slug, 120)
			}
		}
		f.Cause, f.Kind = strings.TrimSpace(f.Cause), ""
		if !validID(f.ID) || ids[f.ID] {
			return nil, fmt.Errorf("finding %d has an invalid or duplicate ID", i+1)
		}
		ids[f.ID] = true
	}
	ids = map[string]bool{}
	for i := range r.Previous {
		p := &r.Previous[i]
		if legacy {
			p.ID = strings.TrimSpace(p.ID)
		}
		if !validID(p.ID) || ids[p.ID] {
			return nil, fmt.Errorf("previous finding %d has an invalid or duplicate ID", i+1)
		}
		ids[p.ID] = true
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
	// The slug is readable but lossy (case, punctuation and Unicode). Hash
	// the original pair so distinct missing IDs do not acquire one identity.
	b, _ := json.Marshal([2]string{f.Path, f.Title})
	return fmt.Sprintf("%s:%s:%x", strings.ToLower(f.Path), slug, sha256.Sum256(b))
}

// validID rejects identities that cannot be displayed verbatim as one line.
// Never repair or shorten an ID: that could alias a different finding.
func validID(id string) bool {
	return id != "" && utf8.ValidString(id) && id == storeText(id, len(id))
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
	// Decision is Joe's comment that justified an accepted finding.
	Decision *DecisionStamp
	// Reason says why a previous finding still blocks.
	Reason string
}

// Assessment is the service's deterministic reading of a report against the
// previous round's blocking findings.
type Assessment struct {
	Conclusion string
	// Incomplete carries missing prior history independently of the model's
	// account of the findings that could be recovered.
	Incomplete bool
	// Overflow carries an earlier review's overflowed checkpoint: its own
	// findings were dropped, so no approval may end Incomplete until a
	// replay restores them.
	Overflow bool
	// TaskID retains the original report for bounded history recovery.
	TaskID string
	// IssueDigest binds the assessment to the service's evidence snapshot.
	IssueDigest string
	// Goal keeps the validated goal answer in the same bounded state.
	Goal *GoalCheck
	// GoalKey identifies the issue even when its goal answer is unavailable.
	GoalKey string
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
	// Advisories are the reviewer's "yes" answers to the HOME-349 invariant
	// scope question. They are notices, not findings, so they never count
	// against MaxLow or the conclusion, and each one is listed.
	Advisories []InvariantScopeAnswer
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
	// Later marks a review of a later head of the PR, whose new blocking
	// findings each name the change that caused them (REVIEW.md "Later
	// heads").
	Later bool
}

// withdrawn reports a note withdrawing a previous finding.
func withdrawn(note string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "withdrawn")
}

// Decisions are Joe's comments on one PR: the trusted comments by a
// `humanApprovers` login, which are Joe's accounts (REVIEW.md "Joe
// decides"). Only they can justify a high finding, or a medium one whose
// note starts "decision:" (REVIEW.md "Clearing a finding without a fix").
type Decisions struct {
	Repo     string
	Number   int
	Comments []github.Comment
	// Carried are the decisions the previous head's pass relied on
	// (State.Decisions). Each stays while its comment is unchanged; a
	// changed one restores its finding (AssessDecided).
	Carried []DecisionStamp
}

// DecisionStamp is Joe's comment that justified finding Finding, with a
// hash of its text then: an edit or a deletion revokes it. Severity is the
// finding's severity then; a stamp without one is high.
type DecisionStamp struct {
	Finding  string `json:"finding,omitempty"`
	ID       int64  `json:"id"`
	Hash     string `json:"hash"`
	Severity string `json:"severity,omitempty"`
}

// severity is the severity a revoked decision restores its finding at.
func (st DecisionStamp) severity() string {
	if st.Severity == SeverityMedium {
		return SeverityMedium
	}
	return SeverityHigh
}

// decisionNote reports a note that claims Joe's decision.
func decisionNote(note string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(note)), "decision:")
}

func bodyHash(body string) string {
	sum := sha256.Sum256([]byte(body))
	return hex.EncodeToString(sum[:])
}

// decided is Joe's comment that note cites to justify finding id at
// severity, or nil. The note starts "decision:" and links the comment on
// this PR (https://github.com/<repo>/pull/<n>#issuecomment-<id>, or
// /issues/), and the comment's text names id. The agent's note alone grants
// nothing.
func (d Decisions) decided(note, id, severity string) *DecisionStamp {
	if d.Repo == "" || d.Number <= 0 || strings.TrimSpace(id) == "" || !decisionNote(note) {
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
				return &DecisionStamp{Finding: id, ID: cid, Hash: bodyHash(c.Body), Severity: severity}
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
// (Decisions.decided). A medium one whose note starts "decision:" needs
// such a comment too, so that an edit or a deletion of it revokes the
// decision. Anything else (open, unaccounted for, a justified critical, a
// justified high or a "decision:" medium without such a comment, a
// justification without a note) still blocks. A previous
// finding reported again keeps the higher of its two severities unless it
// is marked fixed or withdrawn. A previous human-gate finding that is not
// fixed or withdrawn is a human-gate finding of this head too: only a
// person's approval of this head clears it.
func Assess(r *Report, prior []Finding) Assessment {
	return AssessDecided(r, prior, Decisions{})
}

// AssessDecided is Assess with Joe's comments on the PR, which can justify a
// high or a medium finding.
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
	// head, so a medium one raised to high needs Joe's decision. A note
	// that claims Joe's decision is checked and stamped at any severity.
	justified := func(id, severity string) (string, *DecisionStamp, bool) {
		p, wasPrior := priorByID[id]
		s, ok := status[id]
		note := strings.TrimSpace(s.Note)
		if !wasPrior || !ok || s.Status != StatusJustified || note == "" || isGate(p) ||
			(severity != SeverityMedium && severity != SeverityHigh) {
			return note, nil, false
		}
		if severity == SeverityMedium && !decisionNote(note) {
			return note, nil, true
		}
		st := d.decided(note, id, severity)
		return note, st, st != nil
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
	// changed is revoked, and its finding blocks again at the highest
	// severity it was decided at.
	revoked := map[string]string{}
	for _, st := range d.Carried {
		switch {
		case d.holds(st):
			a.Carried = append(a.Carried, st)
		case st.Finding != "":
			if cur, ok := revoked[st.Finding]; !ok || rank(st.severity()) < rank(cur) {
				revoked[st.Finding] = st.severity()
			}
		}
	}
	current := map[string]bool{}
	low := 0
	for _, f := range r.Findings {
		if p, was := priorByID[f.ID]; was && f.Verdict != goalLive && f.Verdict != goalOwned && !cleared(f.ID) && rank(p.Severity) < rank(f.Severity) {
			reason[f.ID] = fmt.Sprintf("reported again as %s; it keeps its earlier severity", f.Severity)
			f.Severity = p.Severity
		}
		if isGate(f) {
			current[f.ID] = true
			a.Gated = append(a.Gated, f)
			continue
		}
		current[f.ID] = true
		if sev, ok := revoked[f.ID]; ok && rank(f.Severity) > rank(sev) {
			reason[f.ID] = fmt.Sprintf("reported again as %s after Joe's decision changed; it stays %s", f.Severity, sev)
			f.Severity, f.Lowered = sev, ""
		}
		switch {
		case f.Severity == SeverityLow && (f.Lowered != "" || f.Verdict == goalOwned):
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
		case s.Status == StatusJustified && decisionNote(note):
			b.Reason = "justified as Joe's decision, but the note does not link Joe's comment on this PR naming it"
		case s.Status == StatusJustified:
			b.Reason = "justified without a note"
		default:
			b.Reason = "still open"
		}
		a.Blocking = append(a.Blocking, b)
	}
	// A revoked decision restores its finding as blocking, unless this
	// review reports it (at the decided severity at least, above) or
	// accounts for it.
	for _, st := range d.Carried {
		sev := revoked[st.Finding]
		if sev == "" || current[st.Finding] {
			continue
		}
		if _, was := priorByID[st.Finding]; was {
			continue
		}
		revoked[st.Finding] = "" // Once, for several stamps of one finding.
		a.Blocking = append(a.Blocking, Blocked{
			Finding: Finding{
				ID: st.Finding, Severity: sev, Title: "Joe's decision on this finding was edited or deleted",
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
	for _, answer := range r.InvariantScopes {
		if answer.Uncovered {
			a.Advisories = append(a.Advisories, answer)
		}
	}
	return a
}

// Scope is what a later head changed since the previous reviewed head. A new
// finding on code that review covered and this head left alone is high,
// critical or low with defer (REVIEW.md "Later heads"); Scope enforces that
// for the medium findings it can place, down to the changed hunks.
type Scope struct {
	paths map[string]bool
	// hunks are the head-side line ranges each changed file's patch
	// touches, with its context lines. A changed file without an entry has
	// no usable patch (binary, too large, renamed from, or rebased), so
	// every line of it counts as changed.
	hunks map[string][]lineRange
	// Rebased marks a scope read from the PR's own diff before and after a
	// rebase (RebasedScope): the base moved too, so a cause need not name a
	// path the PR changed.
	Rebased bool
}

// lineRange is a head-side line range [from, to].
type lineRange struct{ from, to int }

var hunkHeader = regexp.MustCompile(`(?m)^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@`)

// patchHunks reads the head-side line ranges of a unified diff's hunks, or
// nil when the patch is empty or has no hunk header.
func patchHunks(patch string) []lineRange {
	var out []lineRange
	for _, m := range hunkHeader.FindAllStringSubmatch(patch, -1) {
		from, err := strconv.Atoi(m[1])
		if err != nil {
			return nil
		}
		n := 1
		if m[2] != "" {
			if n, err = strconv.Atoi(m[2]); err != nil {
				return nil
			}
		}
		// A pure deletion (n == 0) changes the lines around where it was.
		out = append(out, lineRange{from: max(from, 1), to: max(from+n-1, from)})
	}
	return out
}

// LaterScope is the scope of a head reviewed after since, or nil when there
// is none to enforce: a first review, or an interdiff that is missing,
// truncated or spans a rebase (RebasedScope covers a rebase).
func LaterScope(since string, d *github.CommitComparison) *Scope {
	// An answer with no file list at all, unlike an empty one, shows nothing.
	if since == "" || d == nil || d.Status != "ahead" || d.Truncated || d.Files == nil {
		return nil
	}
	s := &Scope{paths: map[string]bool{}, hunks: map[string][]lineRange{}}
	for _, f := range d.Files {
		s.add(f.Path, patchHunks(f.Patch))
		if f.PreviousPath != "" {
			s.add(f.PreviousPath, nil)
		}
	}
	return s
}

// RebasedScope is the scope of a head reviewed after since when the PR was
// rebased in between: the files whose own diff against the base differs
// between before (since's diff against the base) and after (the head's),
// each changed throughout. It is nil when either list is missing or
// truncated, so the whole change is reviewed again.
func RebasedScope(since string, before, after []github.PullRequestFile, complete bool) *Scope {
	if since == "" || !complete || before == nil || after == nil {
		return nil
	}
	old := map[string]github.PullRequestFile{}
	for _, f := range before {
		old[f.Path] = f
	}
	s := &Scope{paths: map[string]bool{}, hunks: map[string][]lineRange{}, Rebased: true}
	for _, f := range after {
		o, ok := old[f.Path]
		delete(old, f.Path)
		if ok && f.Patch != "" && o.Status == f.Status && o.PreviousPath == f.PreviousPath && samePatch(o.Patch, f.Patch) {
			continue
		}
		s.add(f.Path, nil)
		if f.PreviousPath != "" {
			s.add(f.PreviousPath, nil)
		}
	}
	for p := range old { // A file the PR no longer changes.
		s.add(p, nil)
	}
	return s
}

// samePatch reports two diffs of one file that make the same change, though
// a rebase may have moved their hunks' line numbers.
func samePatch(a, b string) bool {
	strip := func(p string) string { return hunkHeader.ReplaceAllString(p, "@@") }
	return strip(a) == strip(b)
}

func (s *Scope) add(p string, hunks []lineRange) {
	if p = cleanPath(p); p == "" {
		return
	}
	if s.paths[p] {
		delete(s.hunks, p) // Two entries for one path: count it changed throughout.
		return
	}
	s.paths[p] = true
	if hunks != nil {
		s.hunks[p] = hunks
	}
}

// Paths are the changed paths, sorted.
func (s *Scope) Paths() []string {
	if s == nil {
		return nil
	}
	out := make([]string, 0, len(s.paths))
	for p := range s.paths {
		out = append(out, p)
	}
	slices.Sort(out)
	return out
}

// touched reports a finding's place that a change since the previous head
// touched: a line inside a changed hunk of its file, or any place in a
// changed file the service cannot narrow (no line, or no usable patch).
func (s *Scope) touched(p string, line int) bool {
	p = cleanPath(p)
	if p == "" || !s.paths[p] {
		return false
	}
	hunks, ok := s.hunks[p]
	if !ok || line <= 0 {
		return true
	}
	return slices.ContainsFunc(hunks, func(h lineRange) bool { return line >= h.from && line <= h.to })
}

// explains reports a cause that names a path changed since the previous
// head, as a whole word, so the claim points at a change the service can
// see. After a rebase the base moved too, so any stated cause counts.
func (s *Scope) explains(cause string) bool {
	cause = strings.TrimSpace(cause)
	if cause == "" {
		return false
	}
	if s.Rebased {
		return true
	}
	for p := range s.paths {
		if namesPath(cause, p) {
			return true
		}
	}
	return false
}

// namesPath reports text that names path p, or its file name when that is
// unique enough to point at it (it has an extension), as a whole word.
func namesPath(text, p string) bool {
	if namesID(text, p) {
		return true
	}
	base := p[strings.LastIndex(p, "/")+1:]
	return strings.Contains(base, ".") && namesID(text, base)
}

// Lower returns r with each new medium finding lowered to a deferred low
// one when it sits on code an earlier review covered and this head left
// alone: outside every changed hunk, or in an unchanged file. Such a
// finding keeps its severity only when its cause names a change since the
// previous head that introduced or exposed it, such as a caller broken by
// a changed function, and the agent does not mark it earlier. High and
// critical findings, human-gate findings, the previous round's IDs, IDs an
// earlier review recorded as fixed (a regression), findings with no path
// and findings under any rule but lowerableRules keep their severity, so
// neither a missing cause nor a regression missed on an intervening head
// lowers a serious one. A nil Scope lowers nothing.
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
			strings.TrimSpace(f.Path) == "" || !lowerable(*f) || s.touched(f.Path, f.Line) {
			continue
		}
		if !f.Earlier && s.explains(f.Cause) {
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
	statePrefix         = github.ReviewStatePrefix
	stateSuffix         = github.ReviewStateSuffix
)

const incompleteStateMessage = "The complete prior review state is unavailable. Known findings still require accounting, and this check stays blocked. A full review cannot clear missing history. Recovery requires complete history with original reports or verifiably lossless legacy records. If the history cannot be recovered, no review or approval clears this check: open a new PR from commits no review has seen, for example rebased or squashed onto main."

var stateRE = regexp.MustCompile(`<!-- factory-review:state v([12]) ([A-Za-z0-9+/=]*) -->`)

// State keeps a running review's label snapshot, then its blocking
// findings and human gate on the completed check. The next round must
// account for the findings; only a person clears the human gate.
type State struct {
	Round         int                          `json:"round,omitempty"`
	InvariantBase string                       `json:"invariant_base,omitempty"`
	Semgrep       *SemgrepStamp                `json:"semgrep,omitempty"`
	Activation    *github.ActivationAssessment `json:"activation,omitempty"`
	Blocking      []Finding                    `json:"blocking"`
	Cleared       []Cleared                    `json:"cleared,omitempty"`
	// ClearedPartial marks a cleared record that has lost entries, on this
	// head or an earlier one, so it cannot show every fixed finding.
	ClearedPartial bool  `json:"cleared_partial,omitempty"`
	Gate           *Gate `json:"gate,omitempty"`

	// Session is the kagent session that reviewed the head, which the next
	// head's review may resume (session.go).
	Session *SessionRef `json:"session,omitempty"`

	// Decisions are Joe's comments this head's review relied on to accept
	// a finding. Every poll checks they still hold (engine.go).
	Decisions []DecisionStamp `json:"decisions,omitempty"`

	// IssueDigest binds the report to the external evidence it reviewed.
	// IssuesPending revokes that report until a fresh review can run.
	IssueDigest   string `json:"issueDigest,omitempty"`
	IssuesPending bool   `json:"issuesPending,omitempty"`
	// Goal caches the goal check for this head and IssueDigest's evidence.
	Goal *GoalCheck `json:"goal,omitempty"`
	// GoalKey lets recovery account for goal findings without private text.
	GoalKey string `json:"goalKey,omitempty"`

	// Incomplete survives later heads and restarts. Neither a new report nor
	// human-gate reconciliation can establish that missing findings cleared.
	Incomplete bool   `json:"incomplete,omitempty"`
	TaskID     string `json:"task_id,omitempty"`
	// LegacyTask preserves the report's ID derivation if a gate update
	// replaces its original v1 check during migration.
	LegacyTask bool `json:"legacy_task,omitempty"`
	// Overflow marks a checkpoint that dropped this review's own findings to
	// fit. Only a replay of its task clears it.
	Overflow bool `json:"overflow,omitempty"`
	// Trace is the W3C traceparent of the review's root span. A worker that
	// reattaches to the in-progress check after a restart continues it.
	Trace string `json:"trace,omitempty"`
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
	// Decision is Joe's comment that justified a finding, set only by
	// Assess; Decisions.Revalidate clears it when the comment changes.
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
	return render(a, r, gate, footer, nil)
}

// render checks the final persisted state before publishing a verdict. A
// separate state keeps service evidence out of the agent's finding namespace.
func render(a Assessment, r *Report, gate *Gate, footer string, state *State) Outcome {
	if state == nil {
		st := stateOf(a, r, gate)
		state = &st
	}
	o := Outcome{Conclusion: a.Conclusion}
	o.Output.Text = encodeState(*state)
	stored, _ := LoadState(o.Output.Text)
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
	if stored.Incomplete {
		o.Conclusion = ConclusionFailure
		o.Output.Title = "Review state incomplete"
	}

	// The summary is the overview, the blocking findings, the other sections
	// and a tail of the human-hold marker and the footer. The connector reads
	// the marker and the findings, so the tail is never clipped and the
	// blocking findings get the space before the other sections.
	tail := footer
	if !stored.Incomplete && gate != nil && gate.Authorship != "" {
		tail = gate.Authorship + "\n\n" + tail
	}
	if !stored.Incomplete && len(a.Blocking) > 0 && !slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return !isGate(b.Finding) }) {
		tail = gateOnlyMarker + footer
	}
	budget := maxSummary - maxPreface - len(tail)
	var out strings.Builder
	if stored.Incomplete {
		out.WriteString(incompleteStateMessage + "\n\n")
	}
	if sum := strings.TrimSpace(untrusted(r.Summary)); sum != "" {
		out.WriteString(clip(sum, maxAgentSummary))
		out.WriteString("\n\n")
	}
	if r != nil && r.ActivationHead != "" && r.Activation != "" {
		out.WriteString("### Activation\n\n" + r.Activation + "\n\n")
	}
	if len(a.Blocking) > 0 {
		out.WriteString("### Blocking\n\n")
		listed := 0
		for _, e := range blockingEntries(a.Blocking, a.entryContext(), budget-out.Len()-maxOmitted) {
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
		if acts := kindLines(a.Blocking); acts != "" && out.Len()+len(acts) <= budget {
			out.WriteString(acts)
		}
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
		s.WriteString("### Accepted\n\nFindings from the previous review, justified by the agent or by Joe's decision:\n\n")
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
	s.WriteString(advisorySection(a))
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
	for _, n := range a.Advisories {
		add(Finding{Severity: SeverityLow, Path: n.Path, Title: "Invariant scope may be uncovered", Detail: n.Reason})
	}
	o.Output.Annotations = anns
	return o
}

// loweredNote says why Scope lowered a finding.
func loweredNote(f Finding) string {
	return fmt.Sprintf("Reported %s; lowered because the reviewer placed it on code an earlier review covered and this head left alone, "+
		"outside the hunks changed since the previous review, and named no change that introduced or exposed it (REVIEW.md \"Later heads\").",
		oneLine(f.Lowered, 20))
}

// stateOf keeps an assessment's blocking findings for the next round, and
// what this and earlier reviews cleared. The human gate is kept apart: only
// a person clears it, so no round carries it.
func stateOf(a Assessment, r *Report, gate *Gate) State {
	cleared, partial := clearedOf(a, r)
	st := State{Blocking: []Finding{}, Cleared: cleared, ClearedPartial: partial || a.EarlierPartial, Gate: gate, Decisions: a.decisions(), Incomplete: a.Incomplete, Overflow: a.Overflow, TaskID: a.TaskID, IssueDigest: a.IssueDigest, Goal: a.Goal, GoalKey: a.GoalKey}
	if r != nil && r.ActivationHead != "" && r.Activation != "" {
		st.Activation = &github.ActivationAssessment{HeadSHA: r.ActivationHead, Text: r.Activation}
	}
	for _, b := range a.Blocking {
		if isGate(b.Finding) {
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
		id := f.ID
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

// storable bounds descriptive text for the hidden State. Identity and
// severity survive unchanged.
func storable(f Finding) Finding {
	f.Title = storeText(f.Title, maxTitle)
	f.Path = storeText(f.Path, maxStoredPath)
	f.Detail = storeText(f.Detail, maxStoredDetail)
	f.Cause = storeText(f.Cause, maxStoredDetail)
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

// entryContext is what a blocking entry says beyond its finding: whether
// this is a later head, and how earlier heads cleared each finding.
type entryContext struct {
	later   bool
	cleared map[string]string
}

func (a Assessment) entryContext() entryContext {
	c := entryContext{later: a.Later, cleared: map[string]string{}}
	for _, e := range a.Earlier {
		if _, ok := c.cleared[e.ID]; !ok {
			c.cleared[e.ID] = e.Status
		}
	}
	return c
}

// kindLines says what each kind of the blocking findings asks for, one
// line per kind present, defects first.
func kindLines(bs []Blocked) string {
	seen := map[string]bool{}
	for _, b := range bs {
		seen[findingKind(b.Finding)] = true
	}
	var out strings.Builder
	for _, k := range []string{KindDefect, KindEvidence, KindHold} {
		if seen[k] {
			fmt.Fprintf(&out, "- %s\n", kindActions[k])
		}
	}
	if out.Len() == 0 {
		return ""
	}
	return "What each kind asks for:\n\n" + out.String() + "\n"
}

// blockingEntries renders the blocking findings in full when they all fit in
// room. Otherwise it renders every one with its detail and note cut to a
// shared length, down to none, so a revision gets each finding's location
// and title before any finding is left out.
func blockingEntries(bs []Blocked, c entryContext, room int) []string {
	var entries []string
	for _, limit := range []int{maxMessage, 500, 200, 0} {
		entries = entries[:0]
		total := 0
		for _, b := range bs {
			e := blockingEntry(b, c, limit)
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
func blockingEntry(b Blocked, c entryContext, limit int) string {
	var e strings.Builder
	writeFindingN(&e, b.Finding, min(limit, maxDetail))
	if k := findingKind(b.Finding); k != KindDefect {
		fmt.Fprintf(&e, "  Kind: %s.", map[string]string{KindEvidence: "missing evidence", KindHold: "hold"}[k])
	}
	cause := oneLine(b.Cause, min(max(limit, 200), maxMessage))
	status, reopened := c.cleared[b.ID]
	switch {
	case b.Carried:
		fmt.Fprintf(&e, "  From the previous review: %s.", b.Reason)
	case b.Repeated && b.Reason != "":
		fmt.Fprintf(&e, "  Raised in the previous review and still present; %s.", b.Reason)
	case b.Repeated:
		e.WriteString("  Raised in the previous review and still present.")
	case reopened && cause != "":
		fmt.Fprintf(&e, "  Reopened: an earlier head's review recorded it %s. Why: %s", oneLine(status, 20), cause)
	case reopened:
		fmt.Fprintf(&e, "  Reopened: an earlier head's review recorded it %s; the reviewer named no change that brought it back.", oneLine(status, 20))
	case c.later && cause != "":
		fmt.Fprintf(&e, "  Cause on this head: %s", cause)
	case c.later && b.Kind == "" && b.Verdict == "" && !isGate(b.Finding):
		e.WriteString("  New on this head, and the reviewer named no change that caused it: an earlier review missed it.")
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
// the cleared record first, marking it partial if entries are dropped,
// titles and details first, then paths, until only IDs and severities are
// left. If the complete state still cannot fit, it records an explicit
// incomplete marker that holds this and later heads closed.
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
	for _, lim := range []struct{ text, detail, path int }{
		{120, 120, maxStoredPath}, {0, 0, 120}, {0, 0, 0},
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
			if fs == nil {
				return nil // An attempt snapshot is not a completed empty report.
			}
			out := make([]Finding, len(fs))
			for i, f := range fs {
				f.Title, f.Detail, f.Cause = cut(f.Title, lim.text), cut(f.Detail, lim.detail), cut(f.Cause, lim.detail)
				f.Path = cut(f.Path, lim.path)
				if f.Path == "" {
					f.Line = 0
				}
				out[i] = f
			}
			return out
		}
		small := st
		small.Blocking = shrink(st.Blocking)
		small.Cleared, small.ClearedPartial = nil, st.ClearedPartial || len(st.Cleared) > 0
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
	if len(out) > maxState {
		// The task reference is service-owned and bounded. Keep it so a later
		// head can reconstruct every identity from the original report.
		if len(st.TaskID) > 200 {
			st.TaskID = ""
		}
		// A code-review task cannot reconstruct the separate goal answer.
		// Its bounded numbers/verdicts and public issue key must survive too.
		// Keep the service gate's holds and approval baseline. Recovery
		// replays model gate findings from the report, never from here.
		var gate *Gate
		if g := st.Gate; g != nil {
			c := *g
			c.Findings, c.Paths = nil, nil
			for _, p := range g.Paths {
				c.Paths = append(c.Paths, clip(p, 80))
			}
			gate = &c
		}
		checkpoint := State{
			Incomplete: true, Overflow: true, TaskID: st.TaskID, LegacyTask: st.LegacyTask,
			IssueDigest: st.IssueDigest, IssuesPending: st.IssuesPending, Goal: st.Goal, GoalKey: st.GoalKey, Semgrep: st.Semgrep,
			Decisions: st.Decisions, Gate: gate,
		}
		out = enc(checkpoint)
		if len(out) > maxState {
			// Drop decision stamps before the task reference. Replay then
			// reopens each justified high finding until Joe decides again.
			checkpoint.Decisions = nil
			out = enc(checkpoint)
		}
		if len(out) > maxState {
			// Malformed metadata is not a recoverable task checkpoint.
			return enc(State{Incomplete: true, Overflow: true, Gate: gate})
		}
		return out
	}
	return out
}

// LoadState reads the State a completed review kept in its check run text.
// ok is false when the text holds none, as for a review that ended without
// a report. Legacy or invalid state is explicitly incomplete: v1 may have
// silently dropped blockers or clipped identities. Recoverable findings
// still carry forward; only replaying complete original history can clear
// the incomplete flag.
func LoadState(text string) (st State, ok bool) {
	if corruptState(text) {
		return State{Incomplete: true}, true
	}
	m := stateRE.FindStringSubmatch(text)
	if m == nil {
		return State{}, false
	}
	b, _ := base64.StdEncoding.DecodeString(m[2])
	_ = json.Unmarshal(b, &st)
	st.Incomplete = st.Incomplete || m[1] != "2"
	st.LegacyTask = st.LegacyTask || m[1] == "1"
	ids := map[string]int{}
	var recovered []Finding
	for _, f := range st.Previous() {
		if !validID(f.ID) || rank(f.Severity) == len(Severities) || (!isGate(f) && !blocks(f.Severity)) {
			st.Incomplete = true
			continue
		}
		if i, duplicate := ids[f.ID]; duplicate {
			st.Incomplete = true
			if rank(f.Severity) < rank(recovered[i].Severity) {
				recovered[i] = f
			}
			continue
		}
		ids[f.ID] = len(recovered)
		recovered = append(recovered, f)
	}
	if st.Blocking != nil {
		st.Blocking = []Finding{}
	}
	if st.Gate != nil {
		st.Gate.Findings = nil
	}
	for _, f := range recovered {
		if isGate(f) {
			if st.Gate == nil {
				st.Gate = &Gate{}
			}
			st.Gate.Findings = append(st.Gate.Findings, f)
		} else {
			st.Blocking = append(st.Blocking, f)
		}
	}
	return st, true
}

// corruptState reports a review-state block that does not decode, as
// opposed to text that keeps no review state.
func corruptState(text string) bool {
	m := stateRE.FindStringSubmatch(text)
	if m == nil {
		return strings.Contains(text, markerStart+"state ")
	}
	b, err := base64.StdEncoding.DecodeString(m[2])
	var st State
	var probe struct {
		Blocking json.RawMessage `json:"blocking"`
	}
	return err != nil || json.Unmarshal(b, &st) != nil || json.Unmarshal(b, &probe) != nil || probe.Blocking == nil
}

// legacySnapshot certifies only records below both v1 count caps, with
// distinct, unshortened identities. It does not certify their ancestry or
// any resolutions: recovery visits every prior record and carries findings
// absent from a later snapshot until a report explicitly accounts for them.
func legacySnapshot(text string) (State, bool) {
	m := stateRE.FindStringSubmatch(text)
	if m == nil || m[1] != "1" {
		return State{}, false
	}
	b, err := base64.StdEncoding.DecodeString(m[2])
	var st State
	if err != nil || json.Unmarshal(b, &st) != nil || st.Incomplete || st.Blocking == nil || len(st.Blocking) >= 40 ||
		(st.Gate != nil && len(st.Gate.Findings) >= 20) {
		return State{}, false
	}
	seen := map[string]bool{}
	for _, d := range st.Decisions {
		if !validID(d.Finding) || strings.HasSuffix(d.Finding, "…") {
			return State{}, false // A revoked decision must restore its full finding ID.
		}
	}
	for _, f := range st.Previous() {
		if !validID(f.ID) || strings.HasSuffix(f.ID, "…") || seen[f.ID] || rank(f.Severity) == len(Severities) ||
			(!isGate(f) && !blocks(f.Severity)) {
			return State{}, false
		}
		seen[f.ID] = true
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

// advisorySection lists every invariant scope notice. It is not a findings
// list: the review contract's finding limit does not apply to it.
func advisorySection(a Assessment) string {
	if len(a.Advisories) == 0 {
		return ""
	}
	var s strings.Builder
	s.WriteString("### Invariant scope notices\n\n")
	s.WriteString("Advisory: the reviewer thinks these added files may belong to an invariant scope that no rule covers. They do not change the conclusion.\n\n")
	for _, n := range a.Advisories {
		fmt.Fprintf(&s, "- `%s`", oneLine(n.Path, maxTitle))
		if r := oneLine(n.Reason, maxTitle); r != "" {
			s.WriteString(": " + r)
		}
		s.WriteString("\n")
	}
	s.WriteString("\n")
	return s.String()
}
