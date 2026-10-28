-- Admission and replay metadata only. Never store audio, hints or provider text.
CREATE TABLE grimoire.speech_control (
    key varchar(80) PRIMARY KEY,
    window_started double precision NOT NULL DEFAULT 0,
    count integer NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    open_until double precision NOT NULL DEFAULT 0,
    probe_until double precision NOT NULL DEFAULT 0,
    probe_id varchar
);
CREATE TABLE grimoire.speech_attempt (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES grimoire.game_session(id) ON DELETE CASCADE,
    member_id uuid NOT NULL REFERENCES grimoire.campaign_member(id) ON DELETE CASCADE,
    state varchar NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'complete', 'unknown')),
    event_id uuid REFERENCES grimoire.session_event(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
