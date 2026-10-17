-- Retire the WhatsApp gateway's persisted state (#6915).
--
-- Joe chose removal on 2026-10-17 after the home cluster retired (#6914).
-- This change removes the Go gateway, Python inbound/outbox/household handlers,
-- morning digest job, ORM models and chart wiring. No remaining consumer reads
-- or writes the five chat.whatsapp_* tables. Migration dependency review found
-- no shared foreign keys, views, triggers, functions or publications using them.
-- Drop each with default RESTRICT so an unexpected dependency fails loudly.
--
-- whatsmeow created its own versioned whatsmeow_* tables at runtime in schema
-- whatsapp; these are absent from migration history but may survive a database
-- move from home to the hub. Drop only that explicit table prefix together in
-- one RESTRICT statement, which permits foreign keys between the selected
-- tables while refusing dependencies from any other object. RESTRICT the schema
-- drop too: an unexpected object must block retirement, not be silently removed.
--
-- Keep shared chat.goosecracker_sessions provider/checklist columns and
-- chat.goosecracker_steering.author_name: shared consumers still own those tables.
-- Keep dr_jobs.nhs_vacancies.notified_whatsapp too. Pre-rollout pods select the
-- whole Vacancy model in dr_jobs/router.py and need that nullable column until
-- the new image is verified live. Its removal belongs in a later migration.

DROP TABLE IF EXISTS chat.whatsapp_outbox;
DROP TABLE IF EXISTS chat.whatsapp_group;
DROP TABLE IF EXISTS chat.whatsapp_pending_action;
DROP TABLE IF EXISTS chat.whatsapp_reminder;
DROP TABLE IF EXISTS chat.whatsapp_calendar_draft;

DO $$
DECLARE
    owned_tables TEXT;
BEGIN
    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ' ORDER BY tablename)
    INTO owned_tables
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'whatsapp'
      AND tablename LIKE 'whatsmeow\_%' ESCAPE '\';

    IF owned_tables IS NOT NULL THEN
        EXECUTE 'DROP TABLE ' || owned_tables || ' RESTRICT';
    END IF;
END;
$$;

DROP SCHEMA IF EXISTS whatsapp RESTRICT;
