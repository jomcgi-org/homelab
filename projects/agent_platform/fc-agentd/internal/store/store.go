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
	const q = `
		SELECT thread_id, state, repo, branch, node, arch,
		       COALESCE(base_snapshot_ref, ''), COALESCE(thread_snapshot_ref, ''),
		       COALESCE(size_bytes, 0), COALESCE(discord_thread, ''),
		       created_at, last_active_at, COALESCE(ttl_secs, 0)
		  FROM claude_agent.agent_threads
		 WHERE node = $1
		 ORDER BY last_active_at DESC`

	rows, err := s.pool.Query(ctx, q, node)
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
