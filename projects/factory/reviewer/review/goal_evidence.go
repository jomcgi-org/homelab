package review

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"regexp"
	"slices"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

// GoalHistory keeps service-assigned criterion IDs stable when an issue is
// edited. Only keyed digests reach public State, never criterion text. Slots
// remain reserved after removal so a later criterion cannot reuse an ID.
type GoalHistory struct {
	Key       string   `json:"key"`
	Criteria  []string `json:"criteria,omitempty"`
	KeyDigest string   `json:"keyDigest,omitempty"`
}

type GoalCriterion struct {
	Number int
	Text   string
}

type FollowUpEvidence struct {
	Key, Title, Description string
	Column                  itsaplan.Column
	Link                    itsaplan.IssueLink
	BackLink                itsaplan.IssueLink
}

type SwitchEvidence struct {
	Key        string
	Column     itsaplan.Column
	Duplicates []itsaplan.IssueLink
	Retired    bool
}

const maxGoalHistory = 200

var criterionItem = regexp.MustCompile(`^( *)(?:[-+*]|[0-9]+[.)])\s+(?:\[[ xX]\]\s+)?(.+)$`)

// issueCriteria recognizes an explicit acceptance section, or a description
// consisting of a Markdown list. Other formats remain model-numbered and
// cannot prove removal. Nested items and continuation lines stay with their
// parent criterion. Ambiguous sections fail closed.
func issueCriteria(description string) ([]string, bool) {
	lines := strings.Split(strings.ReplaceAll(description, "\r\n", "\n"), "\n")
	start, end := -1, len(lines)
	for n, line := range lines {
		heading := strings.ToLower(strings.Trim(strings.TrimSpace(line), "#* :"))
		if heading == "done when" || heading == "acceptance criteria" {
			if start >= 0 {
				return nil, false
			}
			start = n + 1
		}
	}
	if start < 0 {
		start = 0
		for start < len(lines) && strings.TrimSpace(lines[start]) == "" {
			start++
		}
		if start == len(lines) || criterionItem.FindStringSubmatch(lines[start]) == nil {
			return nil, false
		}
	} else {
		for n := start; n < len(lines); n++ {
			line := strings.TrimSpace(lines[n])
			if strings.HasPrefix(line, "#") || (strings.HasPrefix(line, "**") && strings.HasSuffix(line, "**")) {
				end = n
				break
			}
		}
	}
	var out []string
	indent := -1
	for _, line := range lines[start:end] {
		if strings.Contains(line, "```") || strings.Contains(line, "~~~") || strings.Contains(line, "\t") {
			return nil, false
		}
		if strings.TrimSpace(line) == "" {
			continue
		}
		m := criterionItem.FindStringSubmatch(line)
		if m != nil && (indent < 0 || len(m[1]) == indent) {
			indent = len(m[1])
			out = append(out, m[2])
		} else if len(out) > 0 && len(line)-len(strings.TrimLeft(line, " ")) > indent {
			out[len(out)-1] += "\n" + strings.TrimSpace(line)
		} else {
			return nil, false
		}
	}
	return out, len(out) <= maxCriteria
}

func goalHistory(st State, project string) []GoalHistory {
	out := slices.Clone(st.GoalHistory)
	// Older checks have findings but no inventory. Retain their issue keys
	// for switch evidence, without claiming to know their removed criteria.
	findings := st.Previous()
	for _, f := range st.GoalClosed {
		findings = append(findings, Finding{ID: f.ID})
	}
	for _, f := range findings {
		key, rest, ok := strings.Cut(f.ID, ":goal")
		if ok && (rest == "" || strings.HasPrefix(rest, ":")) && issueNumber(key, project) > 0 &&
			!slices.ContainsFunc(out, func(h GoalHistory) bool { return h.Key == key }) {
			out = append(out, GoalHistory{Key: key})
		}
	}
	return out
}

func (e *Engine) numberCriteria(in *IssueContext) {
	if in.Issue == nil || in.Issue.Clipped {
		return
	}
	texts, known := issueCriteria(in.Issue.Description)
	in.CriteriaKnown = known
	if len(texts) > maxCriteria {
		in.unverified("criteria", "The issue lists more acceptance criteria than the goal check can verify. Reduce the list before review.")
		return
	}
	if !known {
		return
	}
	key, err := e.evidenceKey()
	if err != nil {
		return
	} // sealIssues records the key failure.
	index := slices.IndexFunc(in.History, func(h GoalHistory) bool { return h.Key == in.Issue.Key })
	history := &in.History[index]
	keyMAC := hmac.New(sha256.New, key)
	keyMAC.Write([]byte("criterion inventory key"))
	keyDigest := hex.EncodeToString(keyMAC.Sum(nil)[:16])
	if len(history.Criteria) > 0 && history.KeyDigest != keyDigest {
		in.CriteriaKnown = false
		in.unverified("criteria", "The criterion history was signed with another evidence key. Restore that key to verify criterion identity.")
		return
	}
	history.KeyDigest = keyDigest
	history.Criteria = slices.Clone(history.Criteria)
	total := 0
	for _, h := range in.History {
		total += len(h.Criteria)
	}
	used := map[int]bool{}
	for _, text := range texts {
		mac := hmac.New(sha256.New, key)
		mac.Write([]byte("criterion\x00" + in.Issue.Key + "\x00" + text))
		digest := hex.EncodeToString(mac.Sum(nil)[:16])
		n := -1
		for i, old := range history.Criteria {
			if old == digest && !used[i] {
				n = i
				break
			}
		}
		if n < 0 {
			if total >= maxGoalHistory {
				in.CriteriaKnown, in.Criteria = false, nil
				in.unverified("criteria", "The criterion history exceeds its bound. The service cannot verify criterion identity.")
				return
			}
			n = len(history.Criteria)
			history.Criteria = append(history.Criteria, digest)
			total++
		}
		used[n] = true
		in.Criteria = append(in.Criteria, GoalCriterion{Number: n + 1, Text: text})
	}
}

func issueColumn(p *itsaplan.Project, id int64) (itsaplan.Column, bool) {
	for _, c := range p.Columns {
		if c.ID == id {
			return c, true
		}
	}
	return itsaplan.Column{}, false
}

// Follow-ups come only from the goal issue's tracker links. Read the other
// end for its full scope and current column; require a reciprocal link.
func (e *Engine) followUps(ctx context.Context, b *Binding, p *itsaplan.Project, goal *itsaplan.Issue, in *IssueContext) {
	links := slices.Clone(goal.Links)
	slices.SortFunc(links, func(a, b itsaplan.IssueLink) int { return strings.Compare(a.Issue.Identifier, b.Issue.Identifier) })
	seen := map[string]bool{}
	for _, link := range links {
		key := link.Issue.Identifier
		if key == goal.Identifier || issueNumber(key, b.ItsaplanProject) == 0 || link.Issue.ID <= 0 || link.Issue.Archived || seen[key] {
			continue
		}
		seen[key] = true
		if len(seen) > maxDecisionLinks {
			in.unverified("follow-ups", "The issue has more linked follow-ups than the service can verify. Reduce its links before review.")
			break
		}
		i, err := e.cfg.Itsaplan.GetIssueByNumber(ctx, b.ItsaplanProject, issueNumber(key, b.ItsaplanProject))
		if err != nil && !errors.Is(err, itsaplan.ErrNotFound) {
			in.unread("follow-ups", "A linked follow-up could not be read. The service will retry.")
			continue
		}
		if err != nil || i == nil || i.ID != link.Issue.ID || i.Identifier != key || i.ProjectID != p.Project.ID || i.ArchivedAt != nil {
			continue
		}
		column, ok := issueColumn(p, i.ColumnID)
		if !ok {
			in.unread("follow-ups", "A linked follow-up's workflow column could not be resolved. The service will retry.")
			continue
		}
		if column.StateType != "backlog" && column.StateType != "unstarted" && column.StateType != "started" {
			continue
		}
		back := slices.IndexFunc(i.Links, func(l itsaplan.IssueLink) bool { return l.Issue.ID == goal.ID && l.Issue.Identifier == goal.Identifier })
		if back < 0 {
			continue
		}
		if len(i.Title) > maxPromptTitle || len(i.Description) > maxDecisionText {
			in.unverified("follow-ups", "A linked follow-up exceeds the text bound and supplies no ownership evidence. Shorten its scope before review.")
			continue
		}
		in.FollowUps = append(in.FollowUps, FollowUpEvidence{Key: key, Title: i.Title, Description: i.Description, Column: column, Link: link, BackLink: i.Links[back]})
	}
}

func (e *Engine) issueSwitches(ctx context.Context, b *Binding, p *itsaplan.Project, goal *itsaplan.Issue, in *IssueContext) {
	for _, h := range in.History {
		if h.Key == in.Issue.Key {
			continue
		}
		s := SwitchEvidence{Key: h.Key}
		i, err := e.cfg.Itsaplan.GetIssueByNumber(ctx, b.ItsaplanProject, issueNumber(h.Key, b.ItsaplanProject))
		if err != nil && !errors.Is(err, itsaplan.ErrNotFound) {
			in.unread("switch", "An earlier goal issue could not be read. The service will retry.")
		} else if err == nil && i != nil && i.Identifier == h.Key && i.ProjectID == p.Project.ID {
			var ok bool
			s.Column, ok = issueColumn(p, i.ColumnID)
			if !ok {
				in.unread("switch", "An earlier goal issue's workflow column could not be resolved. The service will retry.")
			}
			s.Retired = ok && s.Column.StateType == "canceled"
			for _, link := range i.Links {
				if link.Kind == "duplicates" && link.Direction == "outward" && link.Issue.ID > 0 &&
					link.Issue.ID == goal.ID && link.Issue.Identifier == goal.Identifier {
					s.Duplicates = append(s.Duplicates, link)
					s.Retired = true
				}
			}
		}
		in.Switches = append(in.Switches, s)
	}
}
