-- Make worldcup.swing_matches country-aware: one row per (match, country)
-- instead of a single focus team (Scotland). The /app/wc2026 page now lets a
-- visitor pick any of the 48 teams, so the refresh sim precomputes each
-- contending team's swing for every remaining match and stores it keyed by
-- country_code. swing_matches holds only ephemeral computed output (the sim
-- delete-then-inserts the whole table each refresh), so dropping and recreating
-- is safe and avoids in-place primary-key surgery.
DROP TABLE IF EXISTS worldcup.swing_matches;

CREATE TABLE worldcup.swing_matches (
    match_id            TEXT NOT NULL,
    country_code        TEXT NOT NULL,
    group_name          TEXT NOT NULL,
    home_code           TEXT NOT NULL,
    away_code           TEXT NOT NULL,
    kickoff             TIMESTAMPTZ,
    swing               DOUBLE PRECISION NOT NULL,
    p_qualify_home_win  DOUBLE PRECISION NOT NULL,
    p_qualify_draw      DOUBLE PRECISION NOT NULL,
    p_qualify_away_win  DOUBLE PRECISION NOT NULL,
    is_own_match        BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, country_code)
);

-- Serves "this country's remaining matches, biggest mover first".
CREATE INDEX idx_worldcup_swing_country_rank
    ON worldcup.swing_matches (country_code, swing DESC);

-- The schema-level ALTER DEFAULT PRIVILEGES (see the original grant migration)
-- only covers tables created by the migration role going forward; an explicit
-- grant on the recreated table keeps public_reader able to read it regardless.
GRANT SELECT ON worldcup.swing_matches TO public_reader;
