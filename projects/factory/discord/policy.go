package discord

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// policyFile is the bridge's policy as values render it.
type policyFile struct {
	NotificationChannels map[string]string `json:"notificationChannels"`
	Guilds               map[string]Guild  `json:"guilds"`
	UserLimit            limitFile         `json:"userLimit"`
	GuildLimit           limitFile         `json:"guildLimit"`
	GlobalLimit          limitFile         `json:"globalLimit"`
	QueueDepth           int               `json:"queueDepth"`
	Operator             *operatorFile     `json:"operator"`
	// ServerAgents maps a guild ID to its agent, "namespace/name" (HOME-909).
	ServerAgents map[string]string `json:"serverAgents"`
	// Users is who may start a turn: "all" (or absent) or "operator", Joe
	// alone (HOME-909).
	Users string `json:"users"`
	// Explore turns on research of public GitHub repositories (HOME-909).
	Explore *exploreFile `json:"explore"`
}

// exploreFile bounds research (explore.go). Every field is required.
type exploreFile struct {
	Agent       string    `json:"agent"`
	Limit       limitFile `json:"limit"`
	Concurrency int       `json:"concurrency"`
	Timeout     string    `json:"timeout"`
	MaxSessions int       `json:"maxSessions"`
	TTL         string    `json:"ttl"`
	MaxRepoMB   int64     `json:"maxRepoMB"`
}

func (e exploreFile) config() (ExploreConfig, error) {
	ns, name, ok := strings.Cut(e.Agent, "/")
	if !ok || ns == "" || name == "" || strings.Contains(name, "/") {
		return ExploreConfig{}, fmt.Errorf("explore: agent %q is not namespace/name", e.Agent)
	}
	lim, err := e.Limit.limit("explore.limit")
	if err != nil {
		return ExploreConfig{}, err
	}
	timeout, err := time.ParseDuration(e.Timeout)
	if err != nil || timeout < time.Minute || timeout > time.Hour {
		return ExploreConfig{}, fmt.Errorf("explore: timeout %q is not 1m to 1h", e.Timeout)
	}
	ttl, err := time.ParseDuration(e.TTL)
	if err != nil || ttl < time.Hour {
		return ExploreConfig{}, fmt.Errorf("explore: ttl %q is under 1h", e.TTL)
	}
	if e.Concurrency < 1 || e.MaxSessions < 1 || e.MaxRepoMB < 1 {
		return ExploreConfig{}, fmt.Errorf("explore: concurrency, maxSessions and maxRepoMB must be positive")
	}
	return ExploreConfig{
		Agent: kagent.AgentRef{Namespace: ns, Name: name}, Limit: lim, Concurrency: e.Concurrency,
		Timeout: timeout, MaxSessions: e.MaxSessions, TTL: ttl, MaxRepoKB: e.MaxRepoMB * 1024,
	}, nil
}

// operatorFile is the operator session's place (HOME-314). Joe's user ID is
// not in the policy: it comes from a Secret file.
type operatorFile struct {
	Guild    string   `json:"guild"`
	Channels []string `json:"channels"`
}

type limitFile struct {
	Count  int    `json:"count"`
	Window string `json:"window"`
}

func (l limitFile) limit(name string) (Limit, error) {
	if l.Count <= 0 {
		return Limit{}, fmt.Errorf("%s: count must be positive", name)
	}
	w, err := time.ParseDuration(l.Window)
	if err != nil || w <= 0 {
		return Limit{}, fmt.Errorf("%s: bad window %q", name, l.Window)
	}
	return Limit{Count: l.Count, Window: w}, nil
}

// ParsePolicy reads the policy file. Every limit is required, so a missing
// value cannot leave spend unbounded (HOME-162 contract rule 7).
func ParsePolicy(data []byte) (Config, error) {
	var p policyFile
	if err := json.Unmarshal(data, &p); err != nil {
		return Config{}, fmt.Errorf("policy: %w", err)
	}
	var c Config
	var err error
	if c.UserLimit, err = p.UserLimit.limit("userLimit"); err != nil {
		return Config{}, err
	}
	if c.GuildLimit, err = p.GuildLimit.limit("guildLimit"); err != nil {
		return Config{}, err
	}
	if c.GlobalLimit, err = p.GlobalLimit.limit("globalLimit"); err != nil {
		return Config{}, err
	}
	c.NotificationChannels = p.NotificationChannels
	c.Guilds = p.Guilds
	for id, g := range c.Guilds {
		if g.IngestSince == "" {
			continue
		}
		if _, err := time.Parse(time.DateOnly, g.IngestSince); err != nil {
			return Config{}, fmt.Errorf("guild %s: ingestSince %q is not YYYY-MM-DD", id, g.IngestSince)
		}
	}
	if c.Guilds == nil {
		c.Guilds = map[string]Guild{}
	}
	c.QueueDepth = max(p.QueueDepth, 1)
	if o := p.Operator; o != nil {
		if o.Guild == "" || len(o.Channels) == 0 {
			return Config{}, fmt.Errorf("operator: guild and channels are required")
		}
		if _, ok := c.Guilds[o.Guild]; !ok {
			return Config{}, fmt.Errorf("operator: guild %s is not allowlisted", o.Guild)
		}
		for _, ch := range o.Channels {
			if ch == "" {
				return Config{}, fmt.Errorf("operator: empty channel ID")
			}
		}
		c.Operator = Operator{Guild: o.Guild, Channels: o.Channels}
	}
	for guild, ref := range p.ServerAgents {
		if _, ok := c.Guilds[guild]; !ok {
			return Config{}, fmt.Errorf("serverAgents: guild %s is not allowlisted", guild)
		}
		ns, name, ok := strings.Cut(ref, "/")
		if !ok || ns == "" || name == "" || strings.Contains(name, "/") {
			return Config{}, fmt.Errorf("serverAgents: guild %s: %q is not namespace/name", guild, ref)
		}
		if c.ServerAgents == nil {
			c.ServerAgents = map[string]kagent.AgentRef{}
		}
		c.ServerAgents[guild] = kagent.AgentRef{Namespace: ns, Name: name}
	}
	if p.Explore != nil {
		if c.Explore, err = p.Explore.config(); err != nil {
			return Config{}, err
		}
	}
	switch p.Users {
	case "", "all":
	case "operator":
		c.OperatorOnly = true
	default:
		return Config{}, fmt.Errorf("users: %q is not all or operator", p.Users)
	}
	return c, nil
}
