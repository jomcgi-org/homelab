package discord

import (
	"encoding/json"
	"sync"
)

// maxPlaceChannels bounds the channels and threads cached for one server.
const maxPlaceChannels = 5000

// Server is the part of a server that a turn's prompt carries.
type Server struct {
	Name        string `json:"name,omitempty"`
	Description string `json:"description,omitempty"`
	// MemberCount is the count from the last GUILD_CREATE. The bridge has no
	// member intent, so the count does not change until the next connection.
	MemberCount int `json:"member_count,omitempty"`
}

// ChannelPlace is the part of a channel that a turn's prompt carries.
// Parent is a thread's parent channel.
type ChannelPlace struct {
	Name   string `json:"name,omitempty"`
	Parent string `json:"parent,omitempty"`
}

// Place is where a message was sent.
type Place struct {
	Server  Server
	Channel ChannelPlace
}

// Places caches servers and their channels from gateway events: GUILD_CREATE,
// GUILD_UPDATE, GUILD_DELETE and the CHANNEL_ and THREAD_ events. A turn reads
// it, so a turn makes no Discord API call for its place.
type Places struct {
	// Keep reports whether to cache a server; nil keeps every server.
	Keep func(guildID string) bool

	mu     sync.RWMutex
	guilds map[string]*placeGuild
}

type placeGuild struct {
	server   Server
	channels map[string]Channel
	// roles maps a role ID to its name; users holds the users seen in the
	// server's messages (Learn).
	roles map[string]string
	users map[string]User
}

type placeChannel struct {
	Channel
	GuildID string `json:"guild_id"`
}

// Apply reads one gateway dispatch. Other event types and bad data change
// nothing.
func (p *Places) Apply(event string, data json.RawMessage) {
	switch event {
	case "GUILD_CREATE", "GUILD_UPDATE":
		var g struct {
			ID          string    `json:"id"`
			Name        string    `json:"name"`
			Description *string   `json:"description"`
			MemberCount int       `json:"member_count"`
			Unavailable bool      `json:"unavailable"`
			Channels    []Channel `json:"channels"`
			Threads     []Channel `json:"threads"`
			Roles       []Role    `json:"roles"`
		}
		if json.Unmarshal(data, &g) != nil || g.ID == "" || g.Unavailable || !p.keep(g.ID) {
			return
		}
		p.mu.Lock()
		defer p.mu.Unlock()
		pg := p.guild(g.ID)
		pg.server.Name = g.Name
		pg.server.Description = ""
		if g.Description != nil {
			pg.server.Description = *g.Description
		}
		pg.roles = map[string]string{}
		for _, r := range g.Roles {
			pg.roles[r.ID] = r.Name
		}
		if event == "GUILD_UPDATE" {
			return // No member count and no channels.
		}
		pg.server.MemberCount = g.MemberCount
		pg.channels = map[string]Channel{}
		for _, c := range append(g.Channels, g.Threads...) {
			pg.put(c)
		}
	case "GUILD_DELETE":
		var g struct {
			ID          string `json:"id"`
			Unavailable bool   `json:"unavailable"`
		}
		if json.Unmarshal(data, &g) != nil || g.Unavailable {
			return // An outage: the server comes back with GUILD_CREATE.
		}
		p.mu.Lock()
		delete(p.guilds, g.ID)
		p.mu.Unlock()
	case "GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE", "GUILD_ROLE_DELETE":
		var e struct {
			GuildID string `json:"guild_id"`
			Role    Role   `json:"role"`
			RoleID  string `json:"role_id"`
		}
		if json.Unmarshal(data, &e) != nil || e.GuildID == "" {
			return
		}
		p.mu.Lock()
		defer p.mu.Unlock()
		pg, ok := p.guilds[e.GuildID]
		if !ok {
			return
		}
		if pg.roles == nil {
			pg.roles = map[string]string{}
		}
		if event == "GUILD_ROLE_DELETE" {
			delete(pg.roles, e.RoleID)
		} else if e.Role.ID != "" && (len(pg.roles) < maxPlaceChannels || pg.roles[e.Role.ID] != "") {
			pg.roles[e.Role.ID] = e.Role.Name
		}
	case "CHANNEL_CREATE", "CHANNEL_UPDATE", "THREAD_CREATE", "THREAD_UPDATE", "CHANNEL_DELETE", "THREAD_DELETE":
		var c placeChannel
		if json.Unmarshal(data, &c) != nil || c.ID == "" || c.GuildID == "" {
			return
		}
		p.mu.Lock()
		defer p.mu.Unlock()
		pg, ok := p.guilds[c.GuildID]
		if !ok {
			return // Not cached, or not kept.
		}
		if event == "CHANNEL_DELETE" || event == "THREAD_DELETE" {
			delete(pg.channels, c.ID)
			return
		}
		pg.put(c.Channel)
	}
}

// Lookup returns the place of a channel in a server. An unknown server or
// channel gives empty fields.
func (p *Places) Lookup(guildID, channelID string) Place {
	if p == nil {
		return Place{}
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	pg, ok := p.guilds[guildID]
	if !ok {
		return Place{}
	}
	c := pg.channels[channelID]
	place := Place{Server: pg.server, Channel: ChannelPlace{Name: c.Name}}
	if c.ParentID != "" && threadTypes[c.Type] {
		place.Channel.Parent = pg.channels[c.ParentID].Name
	}
	return place
}

func (p *Places) keep(guildID string) bool { return p.Keep == nil || p.Keep(guildID) }

// guild returns a server's entry, made if new. p.mu is held.
func (p *Places) guild(id string) *placeGuild {
	if p.guilds == nil {
		p.guilds = map[string]*placeGuild{}
	}
	pg, ok := p.guilds[id]
	if !ok {
		pg = &placeGuild{channels: map[string]Channel{}}
		p.guilds[id] = pg
	}
	return pg
}

// put caches a channel's name and type; past the bound, only known channels
// change. p.mu is held.
func (pg *placeGuild) put(c Channel) {
	if _, ok := pg.channels[c.ID]; !ok && len(pg.channels) >= maxPlaceChannels {
		return
	}
	pg.channels[c.ID] = Channel{ID: c.ID, Name: c.Name, Type: c.Type, ParentID: c.ParentID}
}
