-- Grant public_reader read access to dr_jobs (ADR 004 security/004). The /app/dr-jobs
-- page is served by the public tier (monolith-public), which reads monolith-pg-ro as
-- public_reader. The dr_jobs schema shipped (20260616130000_dr_jobs_schema.sql) without
-- this grant, so every public-tier request 500'd with "permission denied for schema
-- dr_jobs" and the SSR turned that into a 503. NHS Scotland vacancies are wholly public
-- data, so dr_jobs joins hikes/ships/stars as a directly-readable schema (no public_api
-- view needed). ALTER DEFAULT PRIVILEGES covers tables a future migration adds here so
-- the grant does not silently rot. See 20260617000000_public_reader_role.sql.

GRANT USAGE ON SCHEMA dr_jobs TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA dr_jobs TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA dr_jobs
    GRANT SELECT ON TABLES TO public_reader;
