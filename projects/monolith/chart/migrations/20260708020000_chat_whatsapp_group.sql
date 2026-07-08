-- chat.whatsapp_group: the allow-list registry for the WhatsApp household
-- gateway (ADR 039, spec section 6). Only groups that have an enabled row here
-- produce inbound traffic: the gateway filters on its startup config and the
-- monolith inbound endpoint re-checks this table (defense in depth), so a group
-- that is absent or disabled is dropped without processing.
--
-- One row per allow-listed group. tier maps the group to a capability/tool
-- subset (ADR 034); household is the partner group's tier (knowledge, calendar,
-- reminders, no repo/cluster/artifact). ambient toggles the attention gate's
-- ambient classify for the group. directive_seed carries the seed text for the
-- group directive (an empty seed falls back to a built-in household default in
-- the inbound handler). digest_config holds the morning-digest cadence and quiet
-- hours (Phase 5), as JSONB so it can grow without a migration. enabled is a
-- kill switch that stops all traffic without dropping the config or unpairing.
CREATE TABLE chat.whatsapp_group (
    group_jid       TEXT PRIMARY KEY,
    display_name    TEXT,
    tier            TEXT NOT NULL DEFAULT 'household',
    ambient         BOOLEAN NOT NULL DEFAULT true,
    directive_seed  TEXT,
    digest_config   JSONB,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
