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
type Actions struct {
	// React holds 1 to maxReactions emoji for the message that tagged the
	// bot.
	React []string
	// Thread names a public thread started from the tagged message. The
	// reply goes into it.
	Thread string
	// Pin pins the bot's reply.
	Pin bool
	// Explore starts or continues research on a public GitHub repository
	// (explore.go).
	Explore *ExploreRequest
	// Event creates a scheduled event in the message's server (event.go).
	Event *EventRequest
}

// Any reports whether a holds an action.
func (a Actions) Any() bool {
	return len(a.React) > 0 || a.Thread != "" || a.Pin || a.Explore != nil || a.Event != nil
}

// actionBlock is a fenced code block tagged react, thread, pin, explore or
// event.
var actionBlock = regexp.MustCompile("(?s)```[ \\t]*(react|thread|pin|explore|event)[ \\t]*\\r?\\n(.*?)```")

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

// ParseReact reads a react block: {"emoji": ["👍", "🎉"]}, 1 to 3
// different Unicode emoji.
func ParseReact(spec string) ([]string, error) {
	var s struct {
		Emoji []string `json:"emoji"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return nil, err
	}
	if len(s.Emoji) < 1 || len(s.Emoji) > maxReactions {
		return nil, errAction
	}
	for n, e := range s.Emoji {
		if !pollEmoji(e) || contains(s.Emoji[:n], e) {
			return nil, errAction
		}
	}
	return s.Emoji, nil
}

// ParseThread reads a thread block: {"name": "..."}, 1 to 100 characters on
// one line.
func ParseThread(spec string) (string, error) {
	var s struct {
		Name string `json:"name"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return "", err
	}
	name, ok := pollText(s.Name, maxThreadName)
	if !ok {
		return "", errAction
	}
	return name, nil
}

// ParsePin reads a pin block: empty or {}.
func ParsePin(spec string) error {
	if strings.TrimSpace(spec) == "" {
		return nil
	}
	return decodeStrict(spec, &struct{}{})
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
				if emoji, err := ParseReact(spec); err == nil {
					a.React, ok = emoji, true
				}
			}
		case "thread":
			if a.Thread == "" {
				if name, err := ParseThread(spec); err == nil {
					a.Thread, ok = name, true
				}
			}
		case "pin":
			if !a.Pin && ParsePin(spec) == nil {
				a.Pin, ok = true, true
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
	if a.Pin {
		tags = append(tags, "[pinned]")
	}
	if a.Explore != nil {
		tags = append(tags, "[research "+a.Explore.Research+": "+a.Explore.Repo+"]")
	}
	if a.Event != nil {
		tags = append(tags, "[event: "+a.Event.Name+", "+a.Event.Start.Format(time.RFC3339)+"]")
	}
	return strings.Join(tags, " ")
}
