-- public_reader: least-privilege read-only role for the Phase 5 anonymous public
-- service (ADR 004 security/004). Confidentiality is enforced here at the database
-- layer, not just at ingress: the role can read the wholly-public datasets (hikes,
-- ships, stars) and a public-only view over knowledge notes, and nothing else. It
-- is never granted the knowledge, chat, home, scheduler, claude_agent, trips, or
-- todo schemas.
--
-- Role creation: in production the role is created by CNPG (spec.managed.roles,
-- which runs as a superuser), because the Atlas migration runs as the `app` role,
-- which owns the schemas but lacks CREATEROLE. The DO block below is a no-op in
-- production (the role already exists); it is here so the CNPG-less test Postgres
-- (which applies every migration as a superuser) creates the role and can exercise
-- these GRANTs. The GRANTs run as `app`, which owns every object granted.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'public_reader') THEN
        CREATE ROLE public_reader NOLOGIN;
    END IF;
END $$;

-- Wholly-public datasets: read directly. ALTER DEFAULT PRIVILEGES covers tables a
-- future migration adds to these schemas, so the grant does not silently rot.
GRANT USAGE ON SCHEMA hikes, ships, stars TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA hikes, ships, stars TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA hikes, ships, stars
    GRANT SELECT ON TABLES TO public_reader;

-- Knowledge is private by default. Expose only public, non-deleted notes through a
-- view. The view is NOT security_invoker, so it reads knowledge.notes with the view
-- owner's privileges, while public_reader gets SELECT only on the view and no access
-- to the knowledge schema. Private rows are therefore unreachable, and a public note
-- whose visibility flips to private disappears from the view immediately.
CREATE SCHEMA IF NOT EXISTS public_api;
GRANT USAGE ON SCHEMA public_api TO public_reader;

CREATE VIEW public_api.knowledge_notes AS
    SELECT
        note_id,
        title,
        type,
        content,
        indexed_at,
        COALESCE(layout_x_public, layout_x) AS layout_x,
        COALESCE(layout_y_public, layout_y) AS layout_y
    FROM knowledge.notes
    WHERE visibility = 'public'
      AND deleted_at IS NULL;

GRANT SELECT ON public_api.knowledge_notes TO public_reader;
