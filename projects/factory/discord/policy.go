package discord

import (
	"encoding/json"
	"fmt"
	"time"
)

// policyFile is the bridge's policy as values render it.
type policyFile struct {
	Guilds      map[string]Guild `json:"guilds"`
	UserLimit   limitFile        `json:"userLimit"`
	GuildLimit  limitFile        `json:"guildLimit"`
	GlobalLimit limitFile        `json:"globalLimit"`
	QueueDepth  int              `json:"queueDepth"`
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
	c.Guilds = p.Guilds
	if c.Guilds == nil {
		c.Guilds = map[string]Guild{}
	}
	c.QueueDepth = max(p.QueueDepth, 1)
	return c, nil
}
