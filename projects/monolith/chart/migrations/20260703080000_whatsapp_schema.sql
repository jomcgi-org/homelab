-- WhatsApp channel gateway (ADR 039): a dedicated schema for whatsmeow's own
-- sqlstore tables (device session, identity keys, app-state, etc.). whatsmeow
-- creates and migrates its own tables inside this schema at runtime (the Go
-- gateway connects with search_path=whatsapp), so this migration only
-- provisions the schema, never any tables.
--
-- The migration runs as the `app` role, which therefore owns the schema; the
-- gateway connects as the same role, so it can CREATE tables here. Ownership
-- carries the needed access, so no explicit GRANT is required (matching the
-- other schema-provisioning migrations).

CREATE SCHEMA IF NOT EXISTS whatsapp;
