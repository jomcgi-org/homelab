-- Grant public_reader read access to worldcup (ADR 004 security/004). The /app/worldcup
-- page is served by the public tier (monolith-public), which reads monolith-pg-ro as
-- public_reader. World Cup 2026 standings, fixtures, and sim outputs are wholly public
-- data, so worldcup joins hikes/ships/stars/dr_jobs as a directly-readable schema (no
-- public_api view needed). ALTER DEFAULT PRIVILEGES covers tables a future migration adds
-- here so the grant does not silently rot. See 20260617000000_public_reader_role.sql.

GRANT USAGE ON SCHEMA worldcup TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA worldcup TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA worldcup
    GRANT SELECT ON TABLES TO public_reader;
