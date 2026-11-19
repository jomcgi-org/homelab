// Package planner validates final messages before proposal admission.
package planner

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/url"
	"regexp"
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

// Description appends a single JSON-quoted evidence line to trusted instructions.
func (p Proposal) Description(instructions string) string {
	quoted, _ := json.Marshal(p)
	line := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return ' '
		}
		return r
	}, string(quoted))
	return instructions + "\n\n<untrusted-evidence>\n> " + line + "\n</untrusted-evidence>"
}
