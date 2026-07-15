-- Grant public_reader read access to faas.function so the PUBLIC tier
-- (monolith-public) can resolve public functions for jomcgi.dev/functions/<name>
-- (R1 Task 13, ADR agents/045). The faas schema shipped
-- (20260714000000_faas_function.sql) without a public grant because R1 started
-- private-only; the public invocation router now reads the registry as
-- public_reader, so without this grant every public /functions/<name> request
-- would 500 with "permission denied for schema faas" and the router would turn
-- that into a 502/404. See docs/runbooks/public-tier-checklist.md item 1.
--
-- SECURITY: the grant is table-wide (public_reader can SELECT every row), but the
-- public invocation router filters to visibility='public' AND last_smoke_at IS
-- NOT NULL in the query (faas.repository.get_public_function), so a private or
-- un-smoked function is never resolvable on the public origin. The grant is
-- necessary but not sufficient; the row-level filter is the security boundary
-- (checklist item 4). ALTER DEFAULT PRIVILEGES covers a future table this schema
-- adds so the grant does not silently rot (mirrors the trips grant).

GRANT USAGE ON SCHEMA faas TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA faas TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA faas
    GRANT SELECT ON TABLES TO public_reader;
