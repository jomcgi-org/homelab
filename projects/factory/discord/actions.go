package discord

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
)

// Discord limits on the agent's message actions.
const (
	maxReactions  = 3
	maxThreadName = 100
)

// Actor does the message actions of an answer as the bot. A Replier that
// is one runs them after a turn; others leave the blocks as text.
type Actor interface {
	// Post sends a reply and returns the new message's ID.
	Post(ctx context.Context, channelID string, reply Reply) (string, error)
	React(ctx context.Context, channelID, messageID, emoji string) error
	// StartThread starts a public thread from a message and returns its ID.
	StartThread(ctx context.Context, channelID, messageID, name string) (string, error)
	Pin(ctx context.Context, channelID, messageID string) error
}

// Actions are the message actions an answer asked for, one of each kind.
// Each Target is the label of a message in the turn's context (Targets):
// "tagged", "replied" or a channel line's ID. The bridge checks it against
// the turn and ignores a label that is not there.
type Actions struct {
	// React holds 1 to maxReactions emoji for ReactTarget, by default the
	// message that tagged the bot.
	React       []string
	ReactTarget string
	// Thread names a public thread started from ThreadTarget, by default the
	// tagged message. The reply goes into it.
	Thread       string
	ThreadTarget string
	// Pin pins the bot's reply, or PinTarget when it names a message.
	Pin       bool
	PinTarget string
	// ReplyTarget, from a reply block, is the message the answer replies
	// to instead of the tagged message.
	ReplyTarget string
	// Explore starts or continues research on a public GitHub repository
	// (explore.go).
	Explore *ExploreRequest
	// Event creates a scheduled event in the message's server (event.go).
	Event *EventRequest
	// Share asks Joe to confirm a share link for an artifact (share.go).
	Share *ShareRequest
}

// Any reports whether a holds an action.
func (a Actions) Any() bool {
	return len(a.React) > 0 || a.Thread != "" || a.Pin || a.Explore != nil || a.Event != nil || a.Share != nil
}

// TargetTagged and TargetReplied label the tagged message and the message
// it replies to; a channel line's label is "m" and its number.
const (
	TargetTagged  = "tagged"
	TargetReplied = "replied"
)

// maxTargetChars bounds a target label.
const maxTargetChars = 32

// Targets maps the labels of the messages in one turn's context to their
// Discord message IDs. Only these messages can be an action's target.
type Targets map[string]string

// Resolve returns the message ID that label names in this turn, or def
// when label is empty or not in the turn: the target is ignored.
func (t Targets) Resolve(label, def string) (id string, ok bool) {
	if label == "" {
		return def, true
	}
	if id, found := t[label]; found && id != "" {
		return id, true
	}
	return def, false
}

func validTarget(s string) bool {
	return len(s) <= maxTargetChars && !strings.ContainsAny(s, " \t\r\n")
}

// actionBlock is a fenced code block tagged react, thread, pin, reply,
// explore, event or share.
var actionBlock = regexp.MustCompile("(?s)```[ \\t]*(react|thread|pin|reply|explore|event|share)[ \\t]*\\r?\\n(.*?)```")

var errAction = errors.New("discord: invalid action")

// decodeStrict reads one JSON object with no unknown field and no trailing
// data.
func decodeStrict(spec string, v any) error {
	d := json.NewDecoder(strings.NewReader(spec))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if d.More() {
		return errAction
	}
	return nil
}

// ParseReact reads a react block: {"emoji": ["👍", "🎉"], "target": "m3"},
// 1 to 3 different Unicode emoji and an optional target.
func ParseReact(spec string) (emoji []string, target string, err error) {
	var s struct {
		Emoji  []string `json:"emoji"`
		Target string   `json:"target"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return nil, "", err
	}
	if len(s.Emoji) < 1 || len(s.Emoji) > maxReactions || !validTarget(s.Target) {
		return nil, "", errAction
	}
	for n, e := range s.Emoji {
		if !pollEmoji(e) || contains(s.Emoji[:n], e) {
			return nil, "", errAction
		}
	}
	return s.Emoji, s.Target, nil
}

// ParseThread reads a thread block: {"name": "...", "target": "m3"}, a name
// of 1 to 100 characters on one line and an optional target.
func ParseThread(spec string) (name, target string, err error) {
	var s struct {
		Name   string `json:"name"`
		Target string `json:"target"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return "", "", err
	}
	name, ok := pollText(s.Name, maxThreadName)
	if !ok || !validTarget(s.Target) {
		return "", "", errAction
	}
	return name, s.Target, nil
}

// ParsePin reads a pin block: empty, {} (pin the answer) or
// {"target": "m3"} (pin that message).
func ParsePin(spec string) (target string, err error) {
	if strings.TrimSpace(spec) == "" {
		return "", nil
	}
	var s struct {
		Target string `json:"target"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return "", err
	}
	if !validTarget(s.Target) {
		return "", errAction
	}
	return s.Target, nil
}

// ParseReply reads a reply block: {"target": "m3"}, the message the answer
// replies to.
func ParseReply(spec string) (string, error) {
	var s struct {
		Target string `json:"target"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return "", err
	}
	if s.Target == "" || !validTarget(s.Target) {
		return "", errAction
	}
	return s.Target, nil
}

// TakeActions takes the first valid block of each kind out of an answer. A
// block that fails its parser stays as text, and so does a second block of
// a kind already taken: one action of each kind per answer.
func TakeActions(answer string) (string, Actions) {
	var a Actions
	var out strings.Builder
	last := 0
	for _, m := range actionBlock.FindAllStringSubmatchIndex(answer, -1) {
		kind, spec := answer[m[2]:m[3]], answer[m[4]:m[5]]
		ok := false
		switch kind {
		case "react":
			if a.React == nil {
				if emoji, target, err := ParseReact(spec); err == nil {
					a.React, a.ReactTarget, ok = emoji, target, true
				}
			}
		case "thread":
			if a.Thread == "" {
				if name, target, err := ParseThread(spec); err == nil {
					a.Thread, a.ThreadTarget, ok = name, target, true
				}
			}
		case "pin":
			if !a.Pin {
				if target, err := ParsePin(spec); err == nil {
					a.Pin, a.PinTarget, ok = true, target, true
				}
			}
		case "reply":
			if a.ReplyTarget == "" {
				if target, err := ParseReply(spec); err == nil {
					a.ReplyTarget, ok = target, true
				}
			}
		case "explore":
			if a.Explore == nil {
				if req, err := ParseExplore(spec); err == nil {
					a.Explore, ok = &req, true
				}
			}
		case "event":
			if a.Event == nil {
				if e, err := ParseEvent(spec); err == nil {
					a.Event, ok = &e, true
				}
			}
		case "share":
			if a.Share == nil {
				if r, err := ParseShare(spec); err == nil {
					a.Share, ok = &r, true
				}
			}
		}
		if !ok {
			continue
		}
		out.WriteString(answer[last:m[0]])
		last = m[1]
	}
	out.WriteString(answer[last:])
	return strings.TrimSpace(out.String()), a
}

// Tags names the actions that Discord did, for memory: "[thread: name]",
// "[reacted: 👍 🎉]", "[pinned]", "[event: name, start]".
func (a Actions) Tags() string {
	var tags []string
	if a.Thread != "" {
		tags = append(tags, "[thread: "+a.Thread+"]")
	}
	if len(a.React) > 0 {
		tags = append(tags, "[reacted: "+strings.Join(a.React, " ")+"]")
	}
	if a.Pin && a.PinTarget != "" {
		tags = append(tags, "[pinned a message]")
	} else if a.Pin {
		tags = append(tags, "[pinned]")
	}
	if a.Explore != nil {
		tags = append(tags, "[research "+a.Explore.Research+": "+a.Explore.Repo+"]")
	}
	if a.Event != nil {
		tags = append(tags, "[event: "+a.Event.Name+", "+a.Event.Start.Format(time.RFC3339)+"]")
	}
	if a.Share != nil {
		tags = append(tags, "[asked Joe to confirm a share link: "+a.Share.Path+"]")
	}
	return strings.Join(tags, " ")
}
