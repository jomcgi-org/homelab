package main

import (
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/discord"
)

// wireOperatorUser reads Joe's Discord user ID from
// DISCORD_OPERATOR_USER_FILE for the operator session and the policy's
// `users: operator` gate. An unreadable ID leaves it empty, which closes
// both: no operator turn, and under the gate no turn for anyone.
func wireOperatorUser(log *slog.Logger, b *discord.Bridge) {
	if b.Config.Operator.Guild == "" && !b.Config.OperatorOnly {
		return
	}
	uid, err := fileValue(os.Getenv("DISCORD_OPERATOR_USER_FILE"))()
	if err == nil && strings.Trim(uid, "0123456789") != "" {
		err = fmt.Errorf("not a Discord user ID")
	}
	if err != nil {
		log.Error("operator user ID unreadable; operator turns are off, and with users: operator nobody is answered", "error", err)
		return
	}
	b.Config.Operator.UserID = uid
	if b.Config.OperatorOnly {
		log.Info("answering the operator only")
	}
}

// wireOperator sets up Joe's operator session (HOME-314) when the policy
// places one. DISCORD_OPERATOR_AGENT names its kagent Agent, and
// wireOperatorUser has read Joe's Discord user ID. Without that ID the
// operator's channels stay closed: no ingest, no public turn and no
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
	if b.Config.Operator.UserID == "" {
		return nil // wireOperatorUser logged why.
	}
	b.Operator = &discord.KagentOperator{
		A2A:      a2a,
		Sessions: sessions,
		Agent:    kagent.AgentRef{Namespace: ns, Name: name},
	}
	log.Info("operator session on", "agent", agent, "channels", len(b.Config.Operator.Channels))
	return nil
}

// wireServerAgents gives each server in the policy's serverAgents its own
// persistent session on its agent (HOME-909). Other servers keep Bosun.
func wireServerAgents(log *slog.Logger, b *discord.Bridge, a2a *kagent.A2A, sessions *kagent.Sessions) {
	if len(b.Config.ServerAgents) == 0 {
		return
	}
	b.Servers = map[string]discord.SessionAgent{}
	// One client per Agent, so servers that share a session share its cache.
	clients := map[kagent.AgentRef]*discord.KagentOperator{}
	for guild, agent := range b.Config.ServerAgents {
		c, ok := clients[agent]
		if !ok {
			c = &discord.KagentOperator{
				A2A:       a2a,
				Sessions:  sessions,
				Agent:     agent,
				RequestID: discord.ServerRequestID,
			}
			clients[agent] = c
		}
		b.Servers[guild] = c
		session := guild
		if s, ok := b.Config.ServerSessions[guild]; ok {
			session = s
		}
		log.Info("server agent on", "guild", guild, "agent", agent.Namespace+"/"+agent.Name, "session", session)
	}
}
