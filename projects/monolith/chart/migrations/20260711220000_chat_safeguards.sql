-- Bosun trust & safety safeguards (ADR chat/003).
--
-- Per-user trust ledger, moderation event log, and trained-model registry.
-- Heuristic signals and an LLM intent scorer decrement a decaying per-user
-- trust score; below the lockout threshold the bot stops engaging and reacts
-- with the brig emoji instead of replying. Every scored message stores a
-- feature-vector snapshot so the random-forest trainer (shadow mode) has a
-- labeled dataset to learn from.

-- One row per (guild, user): the decaying trust score. score_updated_at is
-- the decay anchor: effective score = min(100, score + recovery * days since).
CREATE TABLE IF NOT EXISTS chat.user_trust (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         TEXT NOT NULL DEFAULT '',
    user_id          TEXT NOT NULL DEFAULT '',
    score            DOUBLE PRECISION NOT NULL DEFAULT 100,
    score_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal_count     INTEGER NOT NULL DEFAULT 0,
    lockout_count    INTEGER NOT NULL DEFAULT 0,
    last_signal_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_trust_guild_user_unique UNIQUE (guild_id, user_id)
);

-- One row per moderation-relevant observation. kind:
--   signal        heuristic pattern(s) fired on a message (label=1)
--   llm_intent    the intent classifier judged a message malicious (label=1)
--   clean_sample  sampled clean message, negative training example (label=0)
--   enforcement   a locked-out user's message was suppressed (brig emoji)
--   lockout       the score crossed below the threshold (transition marker)
--   pardon        a manual reset via the MCP tool (flips recent labels to 0)
-- features_json is the deterministic feature vector (chat.safeguards
-- FEATURE_NAMES order) captured at observation time; label is the training
-- target (NULL on rows that are not training samples). rf_score is the shadow
-- forest's probability at observation time, for later live-vs-shadow review.
CREATE TABLE IF NOT EXISTS chat.moderation_event (
    id               BIGSERIAL PRIMARY KEY,
    guild_id         TEXT NOT NULL DEFAULT '',
    channel_id       TEXT NOT NULL DEFAULT '',
    message_id       TEXT NOT NULL DEFAULT '',
    user_id          TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'signal',
    signal           TEXT NOT NULL DEFAULT '',
    detail           TEXT NOT NULL DEFAULT '',
    delta            DOUBLE PRECISION NOT NULL DEFAULT 0,
    score_after      DOUBLE PRECISION NOT NULL DEFAULT 0,
    features_json    TEXT NOT NULL DEFAULT '[]',
    label            INTEGER,
    rf_score         DOUBLE PRECISION,
    rf_model_version INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT moderation_event_kind_valid CHECK (
        kind IN ('signal', 'llm_intent', 'clean_sample', 'enforcement',
                 'lockout', 'pardon')
    )
);

-- The ledger reads a user's recent history; the trainer scans labeled rows by
-- recency; the pardon flow flips a user's recent labels.
CREATE INDEX IF NOT EXISTS moderation_event_user_idx
    ON chat.moderation_event (guild_id, user_id, created_at);
CREATE INDEX IF NOT EXISTS moderation_event_training_idx
    ON chat.moderation_event (created_at) WHERE label IS NOT NULL;

-- Trained random-forest registry. model_json is the JSON tree ensemble
-- exported by the Firecracker-sandbox trainer (chat.safeguards_forest);
-- inference walks it in pure Python, so no ML dependency ever enters the
-- monolith. status: every fresh train lands as 'shadow' (scores are stamped
-- onto moderation events but never enforced); flipping a row to 'live' is a
-- deliberate manual step; superseded shadow rows are 'retired'.
CREATE TABLE IF NOT EXISTS chat.trust_model (
    id                 BIGSERIAL PRIMARY KEY,
    version            INTEGER NOT NULL UNIQUE,
    status             TEXT NOT NULL DEFAULT 'shadow',
    model_json         TEXT NOT NULL DEFAULT '{}',
    feature_names_json TEXT NOT NULL DEFAULT '[]',
    n_samples          INTEGER NOT NULL DEFAULT 0,
    n_positive         INTEGER NOT NULL DEFAULT 0,
    metrics_json       TEXT NOT NULL DEFAULT '{}',
    trained_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT trust_model_status_valid CHECK (
        status IN ('shadow', 'live', 'retired')
    )
);

CREATE INDEX IF NOT EXISTS trust_model_status_idx
    ON chat.trust_model (status, version);
