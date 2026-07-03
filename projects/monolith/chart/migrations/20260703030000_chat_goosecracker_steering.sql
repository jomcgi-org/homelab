-- Mid-run steering queue for goosecracker agent threads (ADR 035 Phase 2).
--
-- While a turn is running, thread participants can post replies that should
-- steer the in-flight run rather than queue as the next turn (that queuing
-- already exists via goosecracker_sessions.pending). Steering messages are
-- enqueued here by the bot (ACL-gated, Task 2.3) and fetched by the running
-- guest recipe at stage boundaries (Task 2.2), which marks them delivered so
-- a re-poll never redelivers the same message.
--
-- A dedicated table (not reusing pending) because steering is delivered
-- mid-run to a live process over HTTP, not drained by the runner between
-- turns: the access pattern (poll-and-mark-delivered by an external guest)
-- is different enough to warrant its own rows and its own delivered flag.
--
-- Small and append-mostly; safe to run with no data (new table).
CREATE TABLE IF NOT EXISTS chat.goosecracker_steering (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The fetch query selects undelivered rows for a thread ordered by id; this
-- index covers it directly.
CREATE INDEX IF NOT EXISTS goosecracker_steering_thread_delivered_id_idx
    ON chat.goosecracker_steering (thread_id, delivered, id);
