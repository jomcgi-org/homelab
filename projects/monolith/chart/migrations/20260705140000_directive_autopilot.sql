-- Directive autopilot: source provenance + autonomous-action state (ADR chat/007,
-- PR 3 of the /improve-ambient program).
--
-- The autopilot silently applies high-confidence, low-risk channel and personal
-- directive refinements from ambient interaction signals, then self-validates
-- against the next window's reactions and auto-reverts on regression. A human
-- manual tune always wins: source='manual' rows block the autopilot for a
-- cooldown, so out-of-band manual tuning is never clobbered.

-- Provenance on the two directive stores so the manual-precedence rule works and
-- the introspection surface can explain who set each active row. Existing rows
-- backfill to 'seed'. Values in use: seed | observer | autopilot | manual.
ALTER TABLE chat.channel_directive ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'seed';
ALTER TABLE chat.user_style_pref  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'seed';

-- Autopilot decision + self-validation log. One row per autonomous action.
-- The APPLY phase writes a 'pending_validation' row capturing the pre-apply
-- baseline score, the prior version AND its text (so a revert can reinstate
-- without re-deriving), and the supporting episode ids. The VALIDATE phase reads
-- pending rows whose validate_after has passed, recomputes the post-apply score,
-- and flips status to kept / reverted / superseded_manual. Confident-but-ungated
-- findings land as 'proposed' (channel, routed to the human confirm flow),
-- 'suggested' (user, no proposal flow exists), or 'shadow' (kill-switch mode:
-- what the autopilot WOULD have done, mutating nothing).
CREATE TABLE IF NOT EXISTS chat.directive_autopilot (
    id             BIGSERIAL PRIMARY KEY,
    scope_kind     TEXT NOT NULL DEFAULT 'channel',   -- 'channel' | 'user'
    scope_id       TEXT NOT NULL DEFAULT '',          -- channel_id or user_id
    target_version INTEGER NOT NULL DEFAULT 0,         -- directive/pref version applied
    prior_version  INTEGER,                            -- version to restore on revert
    prior_text     TEXT,                               -- text to reinstate on revert
    baseline_json  TEXT NOT NULL DEFAULT '{}',         -- pre-apply score + components
    rationale      TEXT NOT NULL DEFAULT '',
    evidence_json  TEXT NOT NULL DEFAULT '[]',         -- supporting episode ids
    status         TEXT NOT NULL DEFAULT 'pending_validation',
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    validate_after TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT directive_autopilot_scope_valid CHECK (scope_kind IN ('channel', 'user')),
    CONSTRAINT directive_autopilot_status_valid CHECK (
        status IN (
            'pending_validation', 'kept', 'reverted', 'superseded_manual',
            'proposed', 'suggested', 'shadow'
        )
    )
);

-- The validate phase scans pending rows by (status, validate_after); the
-- introspection surface reads the latest action per scope.
CREATE INDEX IF NOT EXISTS directive_autopilot_pending_idx
    ON chat.directive_autopilot (status, validate_after);
CREATE INDEX IF NOT EXISTS directive_autopilot_scope_idx
    ON chat.directive_autopilot (scope_kind, scope_id, created_at);
