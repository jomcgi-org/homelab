package discord

import (
	"context"
	"errors"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Member is the subset of a guild member the bridge reads: a message's
// author member and each mention's partial member carry the server nick.
type Member struct {
	Nick string `json:"nick"`
}

// Role is the subset of a guild role the bridge reads.
type Role struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// UnknownUser replaces a user mention that the bridge cannot name. A raw
// snowflake never leaves the bridge.
const UnknownUser = "@unknown-user"

// Mention forms in Discord message text.
var mentionRE = regexp.MustCompile(`<@!?(\d+)>|<@&(\d+)>|<#(\d+)>|<a?:(\w+):\d+>|</([\p{L}\p{N}_-]+(?: [\p{L}\p{N}_-]+){0,2}):\d+>|<t:(-?\d+)(?::[tTdDfFR])?>`)

// mentionName is how text names a user: @username, and the server nick or
// global display name in brackets when it is not the same.
func mentionName(u User) string {
	if u.Username == "" {
		return UnknownUser
	}
	display := u.GlobalName
	if u.Member != nil && u.Member.Nick != "" {
		display = u.Member.Nick
	}
	if display == "" || display == u.Username {
		return "@" + u.Username
	}
	return "@" + u.Username + " (" + display + ")"
}

// Resolve replaces the mentions in Discord text with readable names:
// <@id> and <@!id> with @username (display name), <@&id> with @role,
// <#id> with #channel, custom emoji with :name:, a command mention with
// /name and a timestamp with its UTC time. users are the message's
// mentions; a user not there comes from the cache in Places, or is
// UnknownUser. @everyone and @here stay as text: every reply sends
// allowed_mentions {"parse": []}.
func (p *Places) Resolve(guildID, text string, users []User) string {
	if !strings.Contains(text, "<") {
		return text
	}
	return mentionRE.ReplaceAllStringFunc(text, func(s string) string {
		g := mentionRE.FindStringSubmatch(s)
		switch {
		case g[1] != "":
			for _, u := range users {
				if u.ID == g[1] {
					return mentionName(u)
				}
			}
			if u, ok := p.user(guildID, g[1]); ok {
				return mentionName(u)
			}
			return UnknownUser
		case g[2] != "":
			if name := p.role(guildID, g[2]); name != "" {
				return "@" + name
			}
			return "@unknown-role"
		case g[3] != "":
			if name := p.Lookup(guildID, g[3]).Channel.Name; name != "" {
				return "#" + name
			}
			return "#unknown-channel"
		case g[4] != "":
			return ":" + g[4] + ":"
		case g[5] != "":
			return "/" + g[5]
		case g[6] != "":
			sec, err := strconv.ParseInt(g[6], 10, 64)
			if err != nil {
				return s
			}
			return time.Unix(sec, 0).UTC().Format(time.RFC3339)
		}
		return s
	})
}

// maxPlaceUsers bounds the users cached for one server.
const maxPlaceUsers = 5000

// Learn caches users seen in a server's messages, so a later mention of
// them outside a mentions array still gets a name.
func (p *Places) Learn(guildID string, users ...User) {
	if p == nil || guildID == "" || !p.keep(guildID) {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	pg := p.guild(guildID)
	if pg.users == nil {
		pg.users = map[string]User{}
	}
	for _, u := range users {
		if u.ID == "" || u.Username == "" {
			continue
		}
		old, ok := pg.users[u.ID]
		if !ok && len(pg.users) >= maxPlaceUsers {
			continue
		}
		if u.Member == nil && old.Member != nil {
			u.Member = old.Member // A history read has no member; keep the nick.
		}
		pg.users[u.ID] = User{ID: u.ID, Username: u.Username, GlobalName: u.GlobalName, Member: u.Member}
	}
}

func (p *Places) user(guildID, id string) (User, bool) {
	if p == nil {
		return User{}, false
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	pg, ok := p.guilds[guildID]
	if !ok {
		return User{}, false
	}
	u, ok := pg.users[id]
	return u, ok
}

func (p *Places) role(guildID, id string) string {
	if p == nil {
		return ""
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	if pg, ok := p.guilds[guildID]; ok {
		return pg.roles[id]
	}
	return ""
}

// userIDs lists the user mentions in text.
func userIDs(text string) []string {
	var out []string
	for _, g := range mentionRE.FindAllStringSubmatch(text, -1) {
		if g[1] != "" {
			out = append(out, g[1])
		}
	}
	return out
}

// UserReader reads a user by ID.
type UserReader interface {
	User(ctx context.Context, id string) (User, error)
}

// User reads a user by ID.
func (r *REST) User(ctx context.Context, id string) (User, error) {
	var u User
	err := r.do(ctx, http.MethodGet, "/users/"+id, nil, &u)
	return u, err
}

// maxUserLookups bounds the REST user reads for one text.
const maxUserLookups = 5

// UserLookup reads users that no message named, once each: a user that
// Discord does not return is remembered as unknown.
type UserLookup struct {
	Users UserReader

	mu      sync.Mutex
	missing map[string]bool
}

// Fill reads the users mentioned in text that are not in users or in the
// cache, and caches them in places. It runs off the gateway's reader only.
func (l *UserLookup) Fill(ctx context.Context, places *Places, guildID, text string, users []User) {
	if l == nil || l.Users == nil || places == nil {
		return
	}
	n := 0
	for _, id := range userIDs(text) {
		if n == maxUserLookups {
			return
		}
		if hasUser(users, id) {
			continue
		}
		if _, ok := places.user(guildID, id); ok {
			continue
		}
		l.mu.Lock()
		skip := l.missing[id]
		l.mu.Unlock()
		if skip {
			continue
		}
		n++
		u, err := l.Users.User(ctx, id)
		var he *HTTPError
		if err != nil && !(errors.As(err, &he) && he.Status == http.StatusNotFound) {
			continue // Transient: try again on a later text.
		}
		if err != nil || u.Username == "" {
			l.mu.Lock()
			if l.missing == nil || len(l.missing) >= maxPlaceUsers {
				l.missing = map[string]bool{}
			}
			l.missing[id] = true
			l.mu.Unlock()
			continue
		}
		u.ID = id
		places.Learn(guildID, u)
	}
}

func hasUser(users []User, id string) bool {
	for _, u := range users {
		if u.ID == id {
			return true
		}
	}
	return false
}
