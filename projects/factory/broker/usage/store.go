// Package usage stores the broker's observations in the connector database.
// Only the connector changes dispatch state; the broker appends call evidence.
package usage

import (
	"context"
	"database/sql"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/relay"
)

type Store struct{ DB *sql.DB }

func (s *Store) Begin(ctx context.Context, actor actorjwt.Identity) (int64, error) {
	// kagent v1.0.1 substrate.ActorName is "session-" + lower(sessionID).
	// Do not trust the caller's Session_id or Conversation_id headers.
	session, ok := strings.CutPrefix(actor.ActorName, "session-")
	if !ok || session == "" {
		return 0, nil
	}
	var id int64
	err := s.DB.QueryRowContext(ctx, `SELECT public.record_codex_call($1, $2, $3)`,
		actor.Atespace, strings.ToLower(session), actor.ActorUID).Scan(&id)
	return id, err
}

func (s *Store) Complete(ctx context.Context, call int64, u relay.TokenUsage) error {
	_, err := s.DB.ExecContext(ctx, `SELECT public.record_codex_usage($1, $2, $3, $4, $5)`,
		call, u.ResponseID, u.Input, u.CachedInput, u.Output)
	return err
}
