package discord

import (
	"regexp"
	"strings"
)

// Components V2 separator type and Discord's limit of 40 components in a
// message.
const (
	componentSeparator = 14
	maxComponents      = 40
	separatorSmall     = 1
)

// ruleLine is a Markdown horizontal rule. Discord shows it as text, so
// Layout turns it into a separator.
var ruleLine = regexp.MustCompile(`^[ \t]*(-{3,}|\*{3,}|_{3,})[ \t]*$`)

// Layout turns each rule line outside a code block into a divider between
// top-level text displays. When the parts pass Discord's component limit,
// the reply keeps its own components. A reply with no Components V2 flag or
// a poll stays the same.
func (r Reply) Layout() Reply {
	if r.Flags&flagComponentsV2 == 0 || r.Poll != nil || len(r.Components) == 0 {
		return r
	}
	var parts []ReplyComponent
	for _, c := range r.Components {
		if c.Type != componentText {
			parts = append(parts, c)
			continue
		}
		for n, text := range splitRules(c.Content) {
			if n > 0 {
				parts = append(parts, divider())
			}
			parts = append(parts, ReplyComponent{Type: componentText, Content: text})
		}
	}
	if len(parts) > 0 && len(parts) <= maxComponents {
		r.Components = parts
	}
	return r
}

// divider is a small gap with a line.
func divider() ReplyComponent {
	line := true
	return ReplyComponent{Type: componentSeparator, Divider: &line, Spacing: separatorSmall}
}

// splitRules cuts text at rule lines outside fenced code blocks and drops
// empty parts.
func splitRules(text string) []string {
	var parts []string
	var cur []string
	fenced := false
	flush := func() {
		if s := strings.TrimSpace(strings.Join(cur, "\n")); s != "" {
			parts = append(parts, s)
		}
		cur = nil
	}
	for _, line := range strings.Split(text, "\n") {
		// A fence line opens or closes a block; ```code``` on one line
		// does neither.
		if t := strings.TrimSpace(line); strings.HasPrefix(t, "```") && strings.Count(t, "```") == 1 {
			fenced = !fenced
		}
		if !fenced && ruleLine.MatchString(line) {
			flush()
			continue
		}
		cur = append(cur, line)
	}
	flush()
	return parts
}
