-- World Cup 2026 Scotland qualification tracker.
-- Standings + fixtures mirrored from worldcup26.ir; sim outputs computed in-cluster.
-- Wholly public data: granted directly to public_reader (see the grant migration),
-- no public_api view needed (mirrors dr_jobs).
CREATE SCHEMA IF NOT EXISTS worldcup;

CREATE TABLE worldcup.standings (
    team_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    fifa_code   TEXT NOT NULL,
    flag_url    TEXT,
    group_name  TEXT NOT NULL,
    mp          INTEGER NOT NULL DEFAULT 0,
    w           INTEGER NOT NULL DEFAULT 0,
    d           INTEGER NOT NULL DEFAULT 0,
    l           INTEGER NOT NULL DEFAULT 0,
    pts         INTEGER NOT NULL DEFAULT 0,
    gf          INTEGER NOT NULL DEFAULT 0,
    ga          INTEGER NOT NULL DEFAULT 0,
    gd          INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_standings_group ON worldcup.standings (group_name);

CREATE TABLE worldcup.fixtures (
    match_id    TEXT PRIMARY KEY,
    group_name  TEXT NOT NULL,
    matchday    INTEGER NOT NULL,
    home_id     TEXT NOT NULL,
    home_name   TEXT NOT NULL,
    home_code   TEXT NOT NULL,
    away_id     TEXT NOT NULL,
    away_name   TEXT NOT NULL,
    away_code   TEXT NOT NULL,
    home_score  INTEGER,
    away_score  INTEGER,
    finished    BOOLEAN NOT NULL DEFAULT FALSE,
    kickoff     TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_fixtures_group ON worldcup.fixtures (group_name);

CREATE TABLE worldcup.qualification (
    team_id        TEXT PRIMARY KEY,
    fifa_code      TEXT NOT NULL,
    prob_qualify   DOUBLE PRECISION NOT NULL,
    prob_top2      DOUBLE PRECISION NOT NULL,
    prob_third     DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL DEFAULT 'contention',
    n_sims         INTEGER NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE worldcup.swing_matches (
    match_id            TEXT PRIMARY KEY,
    focus_team_id       TEXT NOT NULL,
    group_name          TEXT NOT NULL,
    home_code           TEXT NOT NULL,
    away_code           TEXT NOT NULL,
    kickoff             TIMESTAMPTZ,
    swing               DOUBLE PRECISION NOT NULL,
    p_qualify_home_win  DOUBLE PRECISION NOT NULL,
    p_qualify_draw      DOUBLE PRECISION NOT NULL,
    p_qualify_away_win  DOUBLE PRECISION NOT NULL,
    is_own_match        BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_worldcup_swing_rank ON worldcup.swing_matches (swing DESC);
