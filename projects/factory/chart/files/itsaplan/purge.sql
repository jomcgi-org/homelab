-- itsaplan-secret-init's purge step, run only when the operator accepted lost
-- keys: every app_secret row is encrypted with the lost APP_ENCRYPTION_KEY
-- (the OIDC, Google, mail, SCIM and bot settings). itsaplan decrypts the
-- stored OIDC settings before it accepts new ones, so they must go before
-- the bootstrap can write them again.
delete from app_secret;
