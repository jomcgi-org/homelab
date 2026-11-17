// Package discord is the Discord bridge (HOME-162). It reads gateway events,
// answers only messages that tag the bot in allowlisted servers, keeps each
// channel's memory in its own Hindsight tag scope (hindsight-discord), and
// runs each turn on a kagent Agent that holds no memory credential.
//
// Every message, username and attachment is untrusted data from people other
// than Joe (HOME-162 contract rule 1). The bridge chooses the server's bank
// and the channel's tag from the gateway event, never from message text, and
// every reply sends allowed_mentions {"parse": []}, so text cannot ping.
package discord

import "time"

// User is the subset of a Discord user the bridge reads.
type User struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	Bot      bool   `json:"bot"`
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
