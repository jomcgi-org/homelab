package discord

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Discord limits on a guild scheduled event, and the bridge's bounds.
const (
	maxEventName        = 100
	maxEventLocation    = 100
	maxEventDescription = 1000
	maxEventAhead       = 365 * 24 * time.Hour
	maxEventLength      = 7 * 24 * time.Hour
	// defaultEventLength ends an external event that names no end: Discord
	// needs one.
	defaultEventLength = time.Hour

	eventPrivacyGuildOnly = 2
	eventVoice            = 2
	eventExternal         = 3
	channelVoice          = 2
)

// EventRequest is a guild scheduled event that an answer asked for.
// Location makes an external event; Channel names a voice channel.
type EventRequest struct {
	Name        string
	Start, End  time.Time // End is zero when the block names none.
	Location    string
	Channel     string
	Description string
}

// EventCreator creates a guild scheduled event and returns its ID. It needs
// the bot's Create Events permission.
type EventCreator interface {
	CreateEvent(ctx context.Context, guildID string, e EventRequest) (string, error)
}

// pingText is text that names a user, role, channel or everyone.
var pingText = regexp.MustCompile(`@everyone|@here|<[@#][!&]?\d+>`)

// eventText accepts 1 to limit characters with no ping; line is false for a
// description, which can hold line breaks.
func eventText(s string, limit int, line bool) (string, bool) {
	s = strings.TrimSpace(s)
	n := utf8.RuneCountInString(s)
	if n == 0 || n > limit || pingText.MatchString(s) || (line && strings.ContainsAny(s, "\r\n")) {
		return "", false
	}
	return s, true
}

// ParseEvent reads an event block: {"name": "...", "start": RFC 3339 time
// with an offset, "end": optional, "location": "..." or "channel": voice
// channel name, "description": optional}. Check tests the times against the
// clock.
func ParseEvent(spec string) (EventRequest, error) {
	var s struct {
		Name        string  `json:"name"`
		Start       string  `json:"start"`
		End         *string `json:"end"`
		Location    *string `json:"location"`
		Channel     *string `json:"channel"`
		Description *string `json:"description"`
	}
	if err := decodeStrict(spec, &s); err != nil {
		return EventRequest{}, err
	}
	var e EventRequest
	var ok bool
	if e.Name, ok = eventText(s.Name, maxEventName, true); !ok {
		return EventRequest{}, errAction
	}
	var err error
	if e.Start, err = time.Parse(time.RFC3339, s.Start); err != nil {
		return EventRequest{}, err
	}
	if s.End != nil {
		if e.End, err = time.Parse(time.RFC3339, *s.End); err != nil {
			return EventRequest{}, err
		}
		if !e.End.After(e.Start) || e.End.Sub(e.Start) > maxEventLength {
			return EventRequest{}, errAction
		}
	}
	switch {
	case s.Location != nil && s.Channel == nil:
		if e.Location, ok = eventText(*s.Location, maxEventLocation, true); !ok {
			return EventRequest{}, errAction
		}
		if e.End.IsZero() {
			e.End = e.Start.Add(defaultEventLength)
		}
	case s.Channel != nil && s.Location == nil:
		if e.Channel, ok = eventText(strings.TrimPrefix(strings.TrimSpace(*s.Channel), "#"), maxEventName, true); !ok {
			return EventRequest{}, errAction
		}
	default:
		return EventRequest{}, errAction
	}
	if s.Description != nil {
		if e.Description, ok = eventText(*s.Description, maxEventDescription, false); !ok {
			return EventRequest{}, errAction
		}
	}
	return e, nil
}

// Check refuses an event that starts in the past or more than a year ahead.
func (e EventRequest) Check(now time.Time) error {
	if !e.Start.After(now) {
		return errors.New("its start is in the past")
	}
	if e.Start.Sub(now) > maxEventAhead {
		return errors.New("its start is more than a year ahead")
	}
	return nil
}

// errNoVoiceChannel is an event channel name that does not name exactly one
// voice channel.
var errNoVoiceChannel = errors.New("no single voice channel has that name")

// scheduledEvent is the body of Create Guild Scheduled Event.
type scheduledEvent struct {
	Name           string         `json:"name"`
	PrivacyLevel   int            `json:"privacy_level"`
	Start          string         `json:"scheduled_start_time"`
	End            string         `json:"scheduled_end_time,omitempty"`
	Description    string         `json:"description,omitempty"`
	EntityType     int            `json:"entity_type"`
	ChannelID      string         `json:"channel_id,omitempty"`
	EntityMetadata *eventLocation `json:"entity_metadata,omitempty"`
}

type eventLocation struct {
	Location string `json:"location"`
}

// CreateEvent creates a server-only scheduled event in a guild. A voice
// event names its channel by name; the bot looks it up.
func (r *REST) CreateEvent(ctx context.Context, guildID string, e EventRequest) (string, error) {
	body := scheduledEvent{
		Name:         e.Name,
		PrivacyLevel: eventPrivacyGuildOnly,
		Start:        e.Start.UTC().Format(time.RFC3339),
		Description:  e.Description,
	}
	if !e.End.IsZero() {
		body.End = e.End.UTC().Format(time.RFC3339)
	}
	if e.Location != "" {
		body.EntityType = eventExternal
		body.EntityMetadata = &eventLocation{Location: e.Location}
	} else {
		channels, err := r.GuildChannels(ctx, guildID)
		if err != nil {
			return "", err
		}
		for _, c := range channels {
			if c.Type == channelVoice && strings.EqualFold(c.Name, e.Channel) {
				if body.ChannelID != "" {
					return "", errNoVoiceChannel
				}
				body.ChannelID = c.ID
			}
		}
		if body.ChannelID == "" {
			return "", errNoVoiceChannel
		}
		body.EntityType = eventVoice
	}
	var out struct {
		ID string `json:"id"`
	}
	err := r.do(ctx, http.MethodPost, "/guilds/"+guildID+"/scheduled-events", body, &out)
	return out.ID, err
}

// EventURL is a scheduled event's link.
func EventURL(guildID, eventID string) string {
	return "https://discord.com/events/" + guildID + "/" + eventID
}

// markdown escapes Discord Markdown in a line of text.
var markdown = strings.NewReplacer(`\`, `\\`, `*`, `\*`, `_`, `\_`, `~`, `\~`, "`", "\\`", `|`, `\|`, `[`, `\[`, `]`, `\]`)

// createEvent creates m's server's scheduled event and returns the line for
// the reply: the event and its link, or a short notice of why it failed.
func (b *Bridge) createEvent(ctx context.Context, l *slog.Logger, m Message, e EventRequest) (string, bool) {
	refused := func(reason string) (string, bool) {
		return "-# I couldn't create the event: " + reason + ".", false
	}
	c, ok := b.Replies.(EventCreator)
	if !ok {
		return refused("this bridge cannot create events")
	}
	if err := e.Check(b.now()); err != nil {
		l.WarnContext(ctx, "event refused", "reason", err)
		return refused(err.Error())
	}
	id, err := c.CreateEvent(ctx, m.GuildID, e)
	var h *HTTPError
	switch {
	case errors.Is(err, errNoVoiceChannel):
		l.WarnContext(ctx, "event refused", "reason", err, "channel", e.Channel)
		return refused(err.Error())
	case errors.As(err, &h) && h.Status == http.StatusForbidden:
		l.WarnContext(ctx, "event refused: the bot lacks the Create Events permission", "error", err)
		return refused("I lack the Create Events permission here")
	case err != nil || id == "":
		l.WarnContext(ctx, "event failed", "error", err)
		return refused("Discord refused it")
	}
	unix := e.Start.Unix()
	return fmt.Sprintf("📅 **%s** · <t:%d:F> (<t:%d:R>)\n%s", markdown.Replace(e.Name), unix, unix, EventURL(m.GuildID, id)), true
}
