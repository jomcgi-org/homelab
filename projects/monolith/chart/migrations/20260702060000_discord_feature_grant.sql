-- Generic per-server Discord bot feature ACL (ADR 029). One row per grant of a
-- feature (command/capability) to a subject in a scope, within a server:
--   guild_id    Discord server id ("" = any server / global grant)
--   subject_id  Discord user id ("" = everyone in that server)
--   feature     command/capability key: "agent", "artifact", ...
--   scope       feature parameter ("" = whole feature; for "agent" = repo name)
--
-- Allow-list only: a matching row grants access. Empty-string sentinels (not
-- NULL) keep the composite primary key clean and behave identically under the
-- SQLite create_all test fixtures and Postgres. Defaults are seeded by an
-- idempotent app-startup bootstrap (which reads the owner/home-server env), not
-- here, so this migration carries schema only.
CREATE TABLE IF NOT EXISTS chat.discord_feature_grant (
    guild_id   TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL DEFAULT '',
    feature    TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (guild_id, subject_id, feature, scope)
);
