// Package planner validates final messages before proposal admission.
package planner

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"
)

type Proposal struct {
	Title       string
	Body        string
	Evidence    []string
	Fingerprint string
	Priority    string
	Fix         string
}

var (
	fingerprint = regexp.MustCompile(`^[a-z0-9._/-]+(:[a-z0-9._/-]+){3}$`)
	invalid     = errors.New("invalid planner output")
)

// Parse rejects malformed envelopes and counts invalid individual proposals.
func Parse(message []byte, namespace string) ([]Proposal, int, error) {
	var items []json.RawMessage
	if len(message) > 65536 || !utf8.Valid(message) || json.Unmarshal(message, &items) != nil || items == nil {
		return nil, 0, invalid
	}
	var accepted []Proposal
	dropped := 0
	for _, item := range items {
		p, ok := parseItem(item, namespace)
		if !ok {
			dropped++
			continue
		}
		accepted = append(accepted, p)
	}
	return accepted, dropped, nil
}

func parseItem(raw []byte, namespace string) (Proposal, bool) {
	var p Proposal
	fields := map[string]any{"title": &p.Title, "body": &p.Body, "fingerprint": &p.Fingerprint, "fix": &p.Fix, "priority": &p.Priority, "evidence": &p.Evidence}
	d := json.NewDecoder(bytes.NewReader(raw))
	if t, _ := d.Token(); t != json.Delim('{') {
		return p, false
	}
	for d.More() {
		key, _ := d.Token()
		k, ok := key.(string)
		dest := fields[k]
		if !ok || dest == nil || d.Decode(dest) != nil {
			return p, false
		}
		delete(fields, k)
	}
	if len(fields) != 0 || strings.TrimSpace(p.Title) == "" || len(p.Title) > 200 || strings.TrimSpace(p.Body) == "" || len(p.Body) > 2000 || len(p.Fingerprint) > 256 || !fingerprint.MatchString(p.Fingerprint) || strings.Count(namespace, ":") != 1 || !strings.HasPrefix(p.Fingerprint, namespace+":") {
		return p, false
	}
	if len(p.Evidence) == 0 || len(p.Evidence) > 8 || (p.Fix != "agent" && p.Fix != "human") {
		return p, false
	}
	switch p.Priority {
	case "low", "medium", "high", "urgent":
	default:
		return p, false
	}
	for _, link := range p.Evidence {
		u, err := url.ParseRequestURI(link)
		if err != nil || len(link) > 1024 || u.Scheme != "https" || u.Hostname() == "" || u.User != nil {
			return p, false
		}
	}
	return p, true
}

// Disposition applies a reviewed schedule's ceiling after validation.
func (p Proposal) Disposition(ceiling string) string {
	switch ceiling {
	case "delegate":
		if p.Fix == "agent" {
			return "delegate"
		}
		return "joe"
	case "joe":
		return "joe"
	default:
		return "backlog"
	}
}

// MaxDescriptionBytes leaves room in the 8,000-byte coder brief for its
// heading, so the brief never clips inside the evidence block.
const MaxDescriptionBytes = 4000

// EvidenceOpen starts the block. JSON quoting escapes "<", so the quoted
// proposal cannot contain either tag.
const EvidenceOpen, EvidenceClose = "<untrusted-evidence>", "</untrusted-evidence>"

const (
	evidencePrefix = "\n\n" + EvidenceOpen + "\n> "
	evidenceSuffix = "\n" + EvidenceClose
)

// Description appends a single JSON-quoted evidence line to trusted
// instructions. It returns false when the result exceeds MaxDescriptionBytes.
func (p Proposal) Description(instructions string) (string, bool) {
	quoted, _ := json.Marshal(p)
	line := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return ' '
		}
		return r
	}, string(quoted))
	d := instructions + evidencePrefix + line + evidenceSuffix
	return d, len(d) <= MaxDescriptionBytes
}

// FingerprintMarker is the trusted line every filed issue carries after its
// evidence block. A later proposal with the same fingerprint is deduplicated
// while an issue holds the marker open (HOME-548). The fingerprint's
// character set cannot contain "-->".
func FingerprintMarker(fingerprint string) string {
	return "<!-- factory-planner-fp:" + fingerprint + " -->"
}

// exactObject rejects extra, missing, duplicate and differently cased fields.
func exactObject(raw []byte, fields map[string]any) bool {
	d := json.NewDecoder(bytes.NewReader(raw))
	if t, _ := d.Token(); t != json.Delim('{') {
		return false
	}
	for d.More() {
		key, err := d.Token()
		if err != nil {
			return false
		}
		k, ok := key.(string)
		if !ok || fields[k] == nil || d.Decode(fields[k]) != nil {
			return false
		}
		delete(fields, k)
	}
	return len(fields) == 0
}

type Action struct {
	Op, Target, Reason string
	Args               json.RawMessage
}

func validArgs(a Action) bool {
	fields := map[string]any{}
	var priority string
	var position *float64
	var partition [][]int
	switch a.Op {
	case "delegate", "cancel", "grant_round", "recut":
	case "set_priority":
		fields["priority"] = &priority
	case "move":
		fields["position"] = &position
	case "split":
		fields["partition"] = &partition
	default:
		return false
	}
	if !exactObject(a.Args, fields) {
		return false
	}
	switch a.Op {
	case "set_priority":
		return priority == "low" || priority == "medium" || priority == "high"
	case "move":
		return position != nil
	case "split":
		if len(partition) < 2 {
			return false
		}
		seen := map[int]bool{}
		for _, group := range partition {
			if len(group) == 0 {
				return false
			}
			for _, index := range group {
				if index < 1 || seen[index] {
					return false
				}
				seen[index] = true
			}
		}
		return true
	}
	return true
}

// Observe validates all three lists but only returns audit candidates.
func Observe(raw []byte, namespace string, allowed []string, targets map[string]json.RawMessage, prs map[int]bool) ([]Action, int) {
	var actions, proposals, cards []json.RawMessage
	if len(raw) > 65536 || !utf8.Valid(raw) || !json.Valid(raw) || !exactObject(raw, map[string]any{"actions": &actions, "proposals": &proposals, "cards": &cards}) || actions == nil || cards == nil || proposals == nil {
		return nil, 1
	}
	var accepted []Action
	dropped := 0
	for _, item := range actions {
		var a Action
		if !exactObject(item, map[string]any{"op": &a.Op, "target": &a.Target, "reason": &a.Reason, "args": &a.Args}) || !slices.Contains(allowed, a.Op) || targets[a.Target] == nil || !validArgs(a) || strings.TrimSpace(a.Reason) == "" || strings.ContainsRune(a.Reason, 0) {
			dropped++
			continue
		}
		accepted = append(accepted, a)
	}
	for _, item := range proposals {
		if _, ok := parseItem(item, namespace); !ok {
			dropped++
		}
	}
	for _, item := range cards {
		var pr int
		var findings []string
		var recommendation string
		if !exactObject(item, map[string]any{"pr": &pr, "findings": &findings, "recommendation": &recommendation}) || !prs[pr] || len(findings) == 0 || slices.ContainsFunc(findings, func(id string) bool { return strings.TrimSpace(id) == "" }) || strings.TrimSpace(recommendation) == "" {
			dropped++
		}
	}
	return accepted, dropped
}
