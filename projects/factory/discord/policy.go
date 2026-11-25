package discord

import (
	"encoding/json"
	"fmt"
	"time"
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
	return c, nil
}
