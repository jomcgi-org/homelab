package discord

import (
	"bytes"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Discord's poll limits (docs "Poll Create Request Object"). Discord sets no
// minimum answer count; a poll with one answer is not a poll, so the bridge
// asks for two.
const (
	maxPollQuestion = 300
	maxPollAnswer   = 55
	minPollAnswers  = 2
	maxPollAnswers  = 10
	maxPollHours    = 768 // 32 days
	defaultPollHrs  = 24
	maxPollEmoji    = 16 // runes; a ZWJ emoji sequence is several runes
	pollLayout      = 1  // DEFAULT, the only layout
)

// Poll is a Discord poll create request.
type Poll struct {
	Question         PollMedia    `json:"question"`
	Answers          []PollAnswer `json:"answers"`
	Duration         int          `json:"duration"`
	AllowMultiselect bool         `json:"allow_multiselect"`
	LayoutType       int          `json:"layout_type"`
}

// PollMedia is a poll question or answer.
type PollMedia struct {
	Text  string     `json:"text"`
	Emoji *PollEmoji `json:"emoji,omitempty"`
}

// PollEmoji is a Unicode emoji. The agent cannot know a server's custom
// emoji IDs, so the bridge sends only a name.
type PollEmoji struct {
	Name string `json:"name"`
}

// PollAnswer is one poll answer.
type PollAnswer struct {
	PollMedia PollMedia `json:"poll_media"`
}

// pollBlock is a fenced code block tagged `poll`.
var pollBlock = regexp.MustCompile("(?s)```[ \\t]*poll[ \\t]*\\r?\\n(.*?)```")

// pollMark holds a poll's place in Polls' text until Visuals has run.
const pollMark = "\x01"

// pollSpec is what the agent writes in a poll block.
type pollSpec struct {
	Question         string       `json:"question"`
	Answers          []answerSpec `json:"answers"`
	DurationHours    *int         `json:"duration_hours"`
	AllowMultiselect bool         `json:"allow_multiselect"`
}

// answerSpec is an answer: a string, or an object with text and an
// optional emoji.
type answerSpec struct {
	Text  string `json:"text"`
	Emoji string `json:"emoji"`
}

func (a *answerSpec) UnmarshalJSON(data []byte) error {
	if len(data) > 0 && data[0] == '"' {
		return json.Unmarshal(data, &a.Text)
	}
	type plain answerSpec
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	return d.Decode((*plain)(a))
}

var errPoll = errors.New("discord: invalid poll")

// ParsePoll reads a poll block's JSON strictly: an unknown field, trailing
// data or a value past Discord's limits refuses the poll.
func ParsePoll(spec string) (*Poll, error) {
	d := json.NewDecoder(strings.NewReader(spec))
	d.DisallowUnknownFields()
	var s pollSpec
	if err := d.Decode(&s); err != nil {
		return nil, err
	}
	if d.More() {
		return nil, errPoll
	}
	question, ok := pollText(s.Question, maxPollQuestion)
	if !ok || len(s.Answers) < minPollAnswers || len(s.Answers) > maxPollAnswers {
		return nil, errPoll
	}
	hours := defaultPollHrs
	if s.DurationHours != nil {
		hours = *s.DurationHours
	}
	if hours < 1 || hours > maxPollHours {
		return nil, errPoll
	}
	p := &Poll{Question: PollMedia{Text: question}, Duration: hours, AllowMultiselect: s.AllowMultiselect, LayoutType: pollLayout}
	for _, a := range s.Answers {
		text, ok := pollText(a.Text, maxPollAnswer)
		if !ok {
			return nil, errPoll
		}
		media := PollMedia{Text: text}
		if a.Emoji != "" {
			if !pollEmoji(a.Emoji) {
				return nil, errPoll
			}
			media.Emoji = &PollEmoji{Name: a.Emoji}
		}
		p.Answers = append(p.Answers, PollAnswer{PollMedia: media})
	}
	return p, nil
}

// pollText trims s and checks that it is 1 to limit characters on one line.
func pollText(s string, limit int) (string, bool) {
	s = strings.TrimSpace(s)
	n := utf8.RuneCountInString(s)
	return s, n > 0 && n <= limit && !strings.ContainsAny(s, "\r\n")
}

// pollEmoji accepts a short run of non-space, non-letter characters with
// at least one symbol past ASCII: a Unicode emoji such as 🍕 or 1️⃣, never a
// custom emoji's <:name:id>, a word or a bare "1", which Discord refuses.
func pollEmoji(s string) bool {
	if utf8.RuneCountInString(s) > maxPollEmoji {
		return false
	}
	symbol := false
	for _, r := range s {
		if unicode.IsSpace(r) || unicode.IsLetter(r) || r == ':' || r == '<' || r == '>' {
			return false
		}
		// A variation selector or joiner alone is no emoji.
		symbol = symbol || (r > unicode.MaxASCII && r != 0xFE0F && r != 0x200D)
	}
	return symbol
}

// Polls takes the first valid poll block out of an answer and leaves
// pollMark in its place. A block that fails ParsePoll stays as text, and so
// does every block after the first poll: one poll per answer. Call
// PlacePoll on Visuals' text and memo afterwards.
func Polls(answer string) (string, *Poll) {
	answer = strings.ReplaceAll(answer, pollMark, "")
	for _, m := range pollBlock.FindAllStringSubmatchIndex(answer, -1) {
		if p, err := ParsePoll(answer[m[2]:m[3]]); err == nil {
			return answer[:m[0]] + pollMark + answer[m[1]:], p
		}
	}
	return answer, nil
}

// PlacePoll removes pollMark from the text shown and names the poll in the
// memo, as images are named.
func PlacePoll(text, memo string, p *Poll) (string, string) {
	name := ""
	if p != nil {
		name = "[poll: " + p.Question.Text + "]"
	}
	return strings.TrimSpace(strings.ReplaceAll(text, pollMark, "")), strings.TrimSpace(strings.ReplaceAll(memo, pollMark, name))
}

// NewPollReply builds the message that carries a poll: a reply to message
// id that cannot ping anyone. Discord refuses a poll in a Components V2
// message, so it has no flags and no components.
func NewPollReply(p *Poll, replyTo string) Reply {
	r := Reply{Poll: p, AllowedMentions: AllowedMentions{Parse: []string{}}}
	if replyTo != "" {
		r.MessageReference = &MessageReference{MessageID: replyTo}
	}
	return r
}
