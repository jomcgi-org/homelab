-- Grant public_reader read access to campsites (ADR 004). The /app/campsites page is
-- served by the public tier (monolith-public) reading monolith-pg-ro as public_reader.
-- BC Parks availability and Open-Meteo forecast are wholly public data, so campsites joins
-- hikes/ships/stars/dr_jobs/worldcup as a directly readable schema (no public_api view
-- needed). ALTER DEFAULT PRIVILEGES covers future tables. See 20260617000000_public_reader_role.sql.
GRANT USAGE ON SCHEMA campsites TO public_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA campsites TO public_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA campsites
    GRANT SELECT ON TABLES TO public_reader;
