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
	err := s.DB.QueryRowContext(ctx, `
		INSERT INTO codex_call (atespace, session_id, actor_uid, delivery_attempt_id)
		VALUES ($1, $2, $3, (
			SELECT id FROM delivery_attempt
			WHERE kagent_namespace = $1 AND lower(context_id) = $2 AND issues > 0
			ORDER BY id DESC LIMIT 1)) RETURNING id`,
		actor.Atespace, strings.ToLower(session), actor.ActorUID).Scan(&id)
	return id, err
}

func (s *Store) Complete(ctx context.Context, call int64, u relay.TokenUsage) error {
	_, err := s.DB.ExecContext(ctx, `
		INSERT INTO codex_usage (call_id, response_id, input_tokens, cached_input_tokens, output_tokens)
		VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING`, call, u.ResponseID, u.Input, u.CachedInput, u.Output)
	return err
}
