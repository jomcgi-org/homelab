-- Private account management only. No users, application grants, Authentik
-- credentials, role membership or identity backfill are created by this migration.
CREATE SCHEMA platform_auth;
CREATE TABLE platform_auth."user" (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE platform_auth.identity (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES platform_auth."user"(id),
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    UNIQUE (issuer, subject)
);
CREATE INDEX platform_identity_user_idx ON platform_auth.identity (user_id);
CREATE TABLE platform_auth.application_user (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES platform_auth."user"(id),
    application TEXT NOT NULL CHECK (application = 'grimoire'),
    application_user_id TEXT NOT NULL,
    UNIQUE (application, application_user_id),
    UNIQUE (application, user_id)
);
CREATE TABLE platform_auth.invitation (
    id UUID PRIMARY KEY,
    recipient_label TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_delivery',
    token_digest TEXT UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_user_id UUID REFERENCES platform_auth."user"(id),
    accepted_issuer TEXT,
    accepted_subject TEXT,
    identity_activated BOOLEAN NOT NULL DEFAULT false,
    CHECK (status IN ('awaiting_delivery', 'pending', 'accepted', 'revoked')),
    CHECK ((status = 'accepted') = (accepted_user_id IS NOT NULL)),
    CHECK ((status = 'accepted') = (accepted_issuer IS NOT NULL AND accepted_subject IS NOT NULL)),
    CHECK (status NOT IN ('pending', 'accepted') OR token_digest IS NOT NULL)
);
CREATE TABLE platform_auth."grant" (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES platform_auth."user"(id),
    permission TEXT NOT NULL CHECK (permission IN ('grimoire.access', 'grimoire.create_game')),
    issued_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, permission)
);
CREATE TABLE platform_auth.command (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    UNIQUE (actor, request_id)
);
CREATE TABLE platform_auth.audit (
    id UUID PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    request_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX platform_audit_target_idx ON platform_auth.audit (target, created_at);
REVOKE ALL ON SCHEMA platform_auth FROM PUBLIC, public_reader, public_writer;
REVOKE ALL ON ALL TABLES IN SCHEMA platform_auth FROM PUBLIC, public_reader, public_writer;
