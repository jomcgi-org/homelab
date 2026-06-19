-- Grant public_reader read access to trips (ADR 004 security/004). The /app/trips
-- pages are served by the public tier (monolith-public), which reads monolith-pg-ro as
-- public_reader. The trips schema shipped (20260615120000_trips_schema.sql) without
-- this grant, so every public-tier request would 500 with "permission denied for schema
-- trips" and the SSR would turn that into a 503. Road-trip photo journeys are wholly
-- public data, so trips joins hikes/ships/stars/dr_jobs as a directly-readable schema
-- (no public_api view needed). ALTER DEFAULT PRIVILEGES covers tables a future migration
-- adds here so the grant does not silently rot. See 20260617000000_public_reader_role.sql.

GRANT USAGE ON SCHEMA trips TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA trips TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA trips
    GRANT SELECT ON TABLES TO public_reader;
