// Package store is the Postgres-backed AgentThread registry (ADR 022,
// decision 5: the control plane is a Postgres table in the monolith, not a
// Kubernetes CRD). fc-agentd reads desired thread state from here and writes
// actual state back; the monolith MCP catalog reads the same table. The table
// lives in the claude_agent schema alongside the rest of the agent surface.
package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
)

// Thread is one row of agent.agent_threads. It is the durable record keyed by
// the stable ThreadID; node, snapshot refs, and the Discord thread are lookups
// off it.
type Thread struct {
	ThreadID          string
	State             substrate.State
	Repo              string
	Branch            string
	Node              string
	Arch              string
	BaseSnapshotRef   string
	ThreadSnapshotRef string
	SizeBytes         int64
	DiscordThread     string
	CreatedAt         time.Time
	LastActiveAt      time.Time
	TTLSeconds        int
	// Recipe and Task are the work assignment delivered to the guest over vsock.
	Recipe string
	Task   string
	// Tier selects the model substrate the controller injects into the guest
	// (ADR 024): "artifact" reaches Gemini via OpenRouter (key swapped at egress),
	// the default/empty tier reaches in-cluster Qwen. The tier also bounds which
	// secret placeholders the guest holds, so it is the credential trust boundary.
	Tier string
	// WakeRequestedAt is non-zero when a caller has asked an IDLE thread to be
	// restored (the one piece of desired state callers set). Zero means no
	// pending wake.
	WakeRequestedAt time.Time
}

// Store wraps a pgx connection pool scoped to the agent_threads registry.
type Store struct {
	pool *pgxpool.Pool
}

// Open connects to Postgres using the given DSN and verifies connectivity.
func Open(ctx context.Context, dsn string) (*Store, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("store: connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close releases the connection pool.
func (s *Store) Close() {
	if s.pool != nil {
		s.pool.Close()
	}
}

// Ping verifies the database is reachable.
func (s *Store) Ping(ctx context.Context) error {
	return s.pool.Ping(ctx)
}

// ListThreadsForNode returns every thread pinned to the given node. The
// reconcile loop only acts on its own node's threads because FC snapshots are
// node-affine.
func (s *Store) ListThreadsForNode(ctx context.Context, node string) ([]Thread, error) {
	return s.queryThreads(ctx,
		`WHERE node = $1 ORDER BY last_active_at DESC`, node)
}

// ListByStateForNode returns this node's threads in the given lifecycle state.
func (s *Store) ListByStateForNode(ctx context.Context, node string, state substrate.State) ([]Thread, error) {
	return s.queryThreads(ctx,
		`WHERE node = $1 AND state = $2 ORDER BY last_active_at`, node, string(state))
}

// ListWakeRequestedForNode returns this node's IDLE threads with a pending wake
// request (the reconcile loop restores these).
func (s *Store) ListWakeRequestedForNode(ctx context.Context, node string) ([]Thread, error) {
	return s.queryThreads(ctx,
		`WHERE node = $1 AND state = 'IDLE' AND wake_requested_at IS NOT NULL
		 ORDER BY wake_requested_at`, node)
}

// ListIdleExpiredForNode returns this node's IDLE threads whose idle TTL has
// elapsed and which have no pending wake (GC eviction candidates).
func (s *Store) ListIdleExpiredForNode(ctx context.Context, node string) ([]Thread, error) {
	return s.queryThreads(ctx,
		`WHERE node = $1 AND state = 'IDLE' AND wake_requested_at IS NULL
		 AND last_active_at + (ttl_secs || ' seconds')::interval < now()
		 ORDER BY last_active_at`, node)
}

const threadColumns = `thread_id, state, repo, branch, node, arch,
		COALESCE(base_snapshot_ref, ''), COALESCE(thread_snapshot_ref, ''),
		COALESCE(size_bytes, 0), COALESCE(discord_thread, ''),
		created_at, last_active_at, COALESCE(ttl_secs, 0),
		COALESCE(recipe, ''), COALESCE(task, ''), COALESCE(tier, ''),
		COALESCE(wake_requested_at, 'epoch'::timestamptz)`

func (s *Store) queryThreads(ctx context.Context, where string, args ...any) ([]Thread, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+threadColumns+` FROM claude_agent.agent_threads `+where, args...)
	if err != nil {
		return nil, fmt.Errorf("store: list threads: %w", err)
	}
	defer rows.Close()

	var out []Thread
	for rows.Next() {
		var t Thread
		if err := rows.Scan(
			&t.ThreadID, &t.State, &t.Repo, &t.Branch, &t.Node, &t.Arch,
			&t.BaseSnapshotRef, &t.ThreadSnapshotRef, &t.SizeBytes, &t.DiscordThread,
			&t.CreatedAt, &t.LastActiveAt, &t.TTLSeconds,
			&t.Recipe, &t.Task, &t.Tier, &t.WakeRequestedAt,
		); err != nil {
			return nil, fmt.Errorf("store: scan thread: %w", err)
		}
		out = append(out, t)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: iterate threads: %w", err)
	}
	return out, nil
}

// SetState updates a thread's lifecycle state and bumps last_active_at.
func (s *Store) SetState(ctx context.Context, threadID string, state substrate.State) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE claude_agent.agent_threads
		    SET state = $2, last_active_at = now()
		  WHERE thread_id = $1`, threadID, string(state))
	if err != nil {
		return fmt.Errorf("store: set state: %w", err)
	}
	return nil
}

// SetThreadSnapshot records a thread's idle snapshot ref + size and moves it to
// IDLE, clearing any pending wake.
func (s *Store) SetThreadSnapshot(ctx context.Context, threadID, ref string, sizeBytes int64) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE claude_agent.agent_threads
		    SET state = 'IDLE', thread_snapshot_ref = $2, size_bytes = $3,
		        wake_requested_at = NULL, last_active_at = now()
		  WHERE thread_id = $1`, threadID, ref, sizeBytes)
	if err != nil {
		return fmt.Errorf("store: set thread snapshot: %w", err)
	}
	return nil
}

// ClearWake clears a thread's pending wake request and marks it RUNNING.
func (s *Store) ClearWake(ctx context.Context, threadID string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE claude_agent.agent_threads
		    SET state = 'RUNNING', wake_requested_at = NULL, last_active_at = now()
		  WHERE thread_id = $1`, threadID)
	if err != nil {
		return fmt.Errorf("store: clear wake: %w", err)
	}
	return nil
}

// EnqueueDiscordOutbox appends a Discord post to chat.discord_outbox for the
// chat leader's drain loop to deliver (ADR 024 Task 5). channelID is the target
// channel or thread id (a Discord thread is addressed as a channel), so the run
// result lands back in the thread that triggered it. The table lives in the same
// monolith Postgres as the agent registry, so the controller's pool can write it
// directly rather than calling back into the monolith.
func (s *Store) EnqueueDiscordOutbox(ctx context.Context, channelID, content string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO chat.discord_outbox (channel_id, content) VALUES ($1, $2)`,
		channelID, content)
	if err != nil {
		return fmt.Errorf("store: enqueue discord outbox: %w", err)
	}
	return nil
}

// Delete removes a thread row (GC/reclaim, after its bundle is deleted).
func (s *Store) Delete(ctx context.Context, threadID string) error {
	_, err := s.pool.Exec(ctx,
		`DELETE FROM claude_agent.agent_threads WHERE thread_id = $1`, threadID)
	if err != nil {
		return fmt.Errorf("store: delete thread: %w", err)
	}
	return nil
}
