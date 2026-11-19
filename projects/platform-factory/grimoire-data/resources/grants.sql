-- The runtime logins' database grants. The grimoire-db-grants Job runs this
-- file after every sync as the database owner, grimoire_restore, in one
-- transaction. It replaces any grant made by hand.
--
-- grimoire_api and grimoire_worker read and write rows and call nextval.
-- Neither owns an object or can create one. Sequences get USAGE only:
-- UPDATE allows setval, and a setval could rewind a shared key. The restore
-- runs as the owner, so the default privileges also cover the tables it
-- creates later.
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  REVOKE ALL ON SCHEMAS FROM grimoire_api, grimoire_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  GRANT USAGE ON SCHEMAS TO grimoire_api, grimoire_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  REVOKE ALL ON TABLES FROM grimoire_api, grimoire_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO grimoire_api, grimoire_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  REVOKE ALL ON SEQUENCES FROM grimoire_api, grimoire_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore
  GRANT USAGE ON SEQUENCES TO grimoire_api, grimoire_worker;

DO $grants$
DECLARE
  schema_name name;
BEGIN
  FOR schema_name IN
    SELECT nspname FROM pg_namespace
    WHERE nspowner = 'grimoire_restore'::regrole OR nspname = 'public'
  LOOP
    -- PUBLIC keeps USAGE on public, where the operator installs pgvector.
    IF schema_name <> 'public' THEN
      EXECUTE format('REVOKE ALL ON SCHEMA %I FROM grimoire_api, grimoire_worker', schema_name);
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO grimoire_api, grimoire_worker', schema_name);
    END IF;
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM grimoire_api, grimoire_worker', schema_name);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO grimoire_api, grimoire_worker', schema_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM grimoire_api, grimoire_worker', schema_name);
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO grimoire_api, grimoire_worker', schema_name);
  END LOOP;
END
$grants$;
