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

	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
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
	// Resume marks a goosecracker reply that should resume the thread's persisted
	// goose session (ADR 026 Phase 2, Model A) instead of cold-rebuilding from the
	// full transcript. The controller injects GOOSE_RESUME=1 into the guest, which
	// restores the session + prior artifact and runs `goose run --resume`.
	Resume bool
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
		COALESCE(resume, false),
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
			&t.Recipe, &t.Task, &t.Tier, &t.Resume, &t.WakeRequestedAt,
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

// RecordClaimFailure accounts for one failed microVM launch on a PENDING thread
// and decides whether the thread has exhausted its retry budget. It increments
// claim_attempts; once that reaches maxAttempts it marks the thread FAILED
// (terminal) and returns true. Below the cap it leaves the thread PENDING and
// returns false.
//
// Crucially, the below-cap path updates ONLY claim_attempts and never the state
// column, so it does not trip the agent_threads_pending_notify trigger (UPDATE OF
// state). That keeps the retry from waking the loop off its own write: the thread
// stays PENDING and the next reconcile poll re-attempts, so the poll interval is
// the backoff. A maxAttempts <= 1 reproduces the old fail-on-first behaviour
// (attempt 1 reaches the cap immediately).
func (s *Store) RecordClaimFailure(ctx context.Context, threadID string, maxAttempts int) (bool, error) {
	var attempts int
	err := s.pool.QueryRow(ctx,
		`UPDATE claude_agent.agent_threads
		    SET claim_attempts = claim_attempts + 1
		  WHERE thread_id = $1
		  RETURNING claim_attempts`, threadID).Scan(&attempts)
	if err != nil {
		return false, fmt.Errorf("store: record claim failure: %w", err)
	}
	if attempts < maxAttempts {
		return false, nil
	}
	if err := s.SetState(ctx, threadID, substrate.StateFailed); err != nil {
		return false, err
	}
	return true, nil
}

// MarkRunningAfterClaim moves a freshly-launched thread to RUNNING and resets its
// claim_attempts. The reset matters because a RUNNING thread can later be pushed
// back to PENDING (an orphaned snapshotless RUNNING re-inits after a daemon
// restart); without it, retry budget consumed before the successful claim would
// carry over and could fail the re-init prematurely.
func (s *Store) MarkRunningAfterClaim(ctx context.Context, threadID string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE claude_agent.agent_threads
		    SET state = 'RUNNING', claim_attempts = 0, last_active_at = now()
		  WHERE thread_id = $1`, threadID)
	if err != nil {
		return fmt.Errorf("store: mark running after claim: %w", err)
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

// ListenPending subscribes to the agent_threads_pending NOTIFY channel and calls
// onNotify for each notification, so the reconcile loop can wake immediately on a
// new PENDING thread instead of waiting for the next poll tick (ADR 026 Task
// 1.2). It holds one dedicated connection out of the pool for the lifetime of
// the listen and reconnects with a short backoff on error, returning only when
// ctx is cancelled. The 5s reconcile poll remains the safety net, so a dropped
// notification merely delays a claim to the next tick, it never loses one.
func (s *Store) ListenPending(ctx context.Context, onNotify func()) error {
	for ctx.Err() == nil {
		if err := s.listenOnce(ctx, "agent_threads_pending", onNotify); err != nil && ctx.Err() == nil {
			// Back off before reconnecting so a flapping DB cannot hot-loop.
			select {
			case <-ctx.Done():
			case <-time.After(time.Second):
			}
		}
	}
	return ctx.Err()
}

// listenOnce holds one pooled connection, LISTENs, and dispatches notifications
// until the connection or context fails.
func (s *Store) listenOnce(ctx context.Context, channel string, onNotify func()) error {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("store: acquire listen conn: %w", err)
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, "LISTEN "+channel); err != nil {
		return fmt.Errorf("store: LISTEN %s: %w", channel, err)
	}
	for {
		if _, err := conn.Conn().WaitForNotification(ctx); err != nil {
			return err
		}
		onNotify()
	}
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
