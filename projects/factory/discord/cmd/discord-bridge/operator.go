package main

import (
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/discord"
)

// wireOperator sets up Joe's operator session (HOME-314) when the policy
// places one. DISCORD_OPERATOR_AGENT names its kagent Agent, and
// DISCORD_OPERATOR_USER_FILE holds Joe's Discord user ID. Without a readable
// ID the operator's channels stay closed: no ingest, no public turn and no
// operator turn.
func wireOperator(log *slog.Logger, b *discord.Bridge, rest *discord.REST, a2a *kagent.A2A, sessions *kagent.Sessions) error {
	if b.Config.Operator.Guild == "" {
		return nil
	}
	b.Channels, b.History = rest, rest
	agent := os.Getenv("DISCORD_OPERATOR_AGENT")
	if agent == "" {
		log.Warn("operator channels set without DISCORD_OPERATOR_AGENT; operator turns are off")
		return nil
	}
	ns, name, ok := strings.Cut(agent, "/")
	if !ok || ns == "" || name == "" {
		return fmt.Errorf("DISCORD_OPERATOR_AGENT %q is not namespace/name", agent)
	}
	uid, err := fileValue(os.Getenv("DISCORD_OPERATOR_USER_FILE"))()
	if err == nil && strings.Trim(uid, "0123456789") != "" {
		err = fmt.Errorf("not a Discord user ID")
	}
	if err != nil {
		log.Error("operator user ID unreadable; operator turns are off", "error", err)
		return nil
	}
	b.Config.Operator.UserID = uid
	b.Operator = &discord.KagentOperator{
		A2A:      a2a,
		Sessions: sessions,
		Agent:    kagent.AgentRef{Namespace: ns, Name: name},
	}
	log.Info("operator session on", "agent", agent, "channels", len(b.Config.Operator.Channels))
	return nil
}
