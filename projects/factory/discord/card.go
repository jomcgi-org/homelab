package discord

import (
	"regexp"
	"strings"
)

// Components V2 layout types and limits. A message holds at most 40
// components, nested ones included; a container cannot hold a container.
const (
	componentSeparator = 14
	componentContainer = 17
	maxComponents      = 40
	separatorSmall     = 1
)

// Card accent colours, from the renderer's house theme (render/theme).
const (
	AccentAnswer   = 0x2A78D6 // blue: an answer
	AccentResearch = 0x8E5BD6 // violet: an explorer's research result
	AccentNotice   = 0xEDA100 // amber: busy, limited and other notices
	AccentFailure  = 0xE34948 // red: the bridge could not answer or act
)

// failures are the fixed replies that get the failure accent.
var failures = map[string]bool{
	ReplyFailed: true, ReplyPollFailed: true, ReplyActionFailed: true, ReplyExploreFailed: true,
}

// noticeAccent is the accent of a fixed reply.
func noticeAccent(text string) int {
	if failures[text] {
		return AccentFailure
	}
	return AccentNotice
}

// ruleLine is a Markdown horizontal rule. Discord shows it as text, so the
// card turns it into a separator.
var ruleLine = regexp.MustCompile(`^[ \t]*(-{3,}|\*{3,}|_{3,})[ \t]*$`)

// Card wraps a Components V2 reply in one container with r.Accent as its
// accent colour. A rule line outside a code block becomes a divider, and a
// small space without a line parts text from images. When the parts pass
// Discord's component limit, the card keeps the reply's own components.
// A reply with no accent, no Components V2 flag or a poll stays the same.
func (r Reply) Card() Reply {
	if r.Accent == 0 || r.Flags&flagComponentsV2 == 0 || r.Poll != nil || len(r.Components) == 0 {
		return r
	}
	var parts []ReplyComponent
	for _, c := range r.Components {
		if c.Type != componentText {
			if len(parts) > 0 {
				parts = append(parts, separator(false))
			}
			parts = append(parts, c)
			continue
		}
		for n, text := range splitRules(c.Content) {
			if len(parts) > 0 {
				parts = append(parts, separator(n > 0))
			}
			parts = append(parts, ReplyComponent{Type: componentText, Content: text})
		}
	}
	if 1+len(parts) > maxComponents || len(parts) == 0 {
		parts = r.Components
	}
	r.Components = []ReplyComponent{{Type: componentContainer, AccentColor: r.Accent, Components: parts}}
	return r
}

// separator is a small gap, with a line when divider is true.
func separator(divider bool) ReplyComponent {
	return ReplyComponent{Type: componentSeparator, Divider: &divider, Spacing: separatorSmall}
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
		if strings.HasPrefix(strings.TrimSpace(line), "```") {
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
