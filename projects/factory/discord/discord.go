// Package discord is the Discord bridge (HOME-162). It reads gateway events,
// answers only messages that tag the bot in allowlisted servers, keeps each
// server's memory in its own Hindsight bank (hindsight-discord), and runs
// each turn on a kagent Agent that holds no memory credential.
//
// Every message, username and attachment is untrusted data from people other
// than Joe (HOME-162 contract rule 1). Joe's operator session (operator.go)
// is the one exception: it takes only his own messages. The bridge chooses the server's bank
// and the channel's tag from the gateway event, never from message text, and
// every reply sends allowed_mentions {"parse": []}, so text cannot ping.
package discord

import (
	"strconv"
	"time"
)

// discordEpoch is the time of snowflake 0.
const discordEpoch = 1420070400000

// SentAt is when a message was sent: its timestamp, or else the time in its
// snowflake ID. An unreadable ID is the zero time, which is old.
func SentAt(m Message) time.Time {
	if !m.Timestamp.IsZero() {
		return m.Timestamp
	}
	id, err := strconv.ParseUint(m.ID, 10, 64)
	if err != nil {
		return time.Time{}
	}
	return time.UnixMilli(int64(id>>22) + discordEpoch)
}

// User is the subset of a Discord user the bridge reads.
type User struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	// GlobalName is the user's display name; Member, in a mention, holds
	// the server nick.
	GlobalName string  `json:"global_name"`
	Bot        bool    `json:"bot"`
	Member     *Member `json:"member,omitempty"`
}

// Message is the subset of a Discord MESSAGE_CREATE the bridge reads.
type Message struct {
	ID        string    `json:"id"`
	ChannelID string    `json:"channel_id"`
	GuildID   string    `json:"guild_id"`
	Author    User      `json:"author"`
	Content   string    `json:"content"`
	Timestamp time.Time `json:"timestamp"`
	Mentions  []User    `json:"mentions"`
	WebhookID string    `json:"webhook_id"`
	// Type is 0 for a message and 19 for a reply; other values are system
	// messages.
	Type int `json:"type"`
	// Reference and Snapshots mark a forwarded message.
	Reference *Reference `json:"message_reference,omitempty"`
	Snapshots []struct{} `json:"message_snapshots,omitempty"`
	// Referenced is the message a reply (type 19) answers. Only a public
	// turn reads it, under the users: operator gate (HOME-909); the
	// operator session never does.
	Referenced *Referenced `json:"referenced_message,omitempty"`
	// Member is the author's server member; it holds the author's nick.
	Member *Member `json:"member,omitempty"`
}

// Users is the message's author, with its nick, and its mentions: the users
// its text names.
func (m Message) Users() []User {
	a := m.Author
	if m.Member != nil {
		a.Member = m.Member
	}
	return append([]User{a}, m.Mentions...)
}

// Referenced is the subset of a replied-to message the bridge reads.
type Referenced struct {
	ID      string `json:"id"`
	Author  User   `json:"author"`
	Content string `json:"content"`
	// Mentions are the users its text names.
	Mentions []User `json:"mentions"`
}

// RepliesTo reports whether m is a reply to a message user id wrote, with
// or without the reply ping.
func (m Message) RepliesTo(id string) bool {
	return id != "" && m.Type == 19 && m.Referenced != nil && m.Referenced.Author.ID == id
}

// Reference is the subset of a received message_reference the bridge reads.
// Type 1 is a forward.
type Reference struct {
	Type int `json:"type"`
}

// Forwarded reports whether m forwards another message.
func (m Message) Forwarded() bool {
	return len(m.Snapshots) > 0 || (m.Reference != nil && m.Reference.Type == 1)
}

// MentionsUser reports whether the message tags user id: Discord lists the
// tagged users in mentions, and the text holds <@id> or <@!id>.
func (m Message) MentionsUser(id string) bool {
	if id == "" {
		return false
	}
	for _, u := range m.Mentions {
		if u.ID == id {
			return true
		}
	}
	return false
}
