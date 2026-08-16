CREATE TABLE agent_sessions.voice_ui_companions (
    id                  TEXT PRIMARY KEY,
    session_id          INTEGER,
    principal_subject   TEXT NOT NULL,
    principal_authority TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ
);

CREATE TABLE agent_sessions.voice_ui_ledger (
    id                  BIGSERIAL PRIMARY KEY,
    companion_id        TEXT NOT NULL,
    session_id          INTEGER,
    call                TEXT NOT NULL
                        CHECK (call IN ('attach', 'show', 'ask', 'dismiss')),
    payload             JSONB NOT NULL DEFAULT '{}',
    principal_subject   TEXT NOT NULL,
    principal_authority TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX voice_ui_ledger_companion_id_id_idx
    ON agent_sessions.voice_ui_ledger (companion_id, id);
