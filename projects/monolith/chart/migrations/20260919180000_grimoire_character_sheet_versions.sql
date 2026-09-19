CREATE TABLE grimoire.character_sheet_version (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id         UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    player_character_id UUID NOT NULL REFERENCES grimoire.player_character(id) ON DELETE CASCADE,
    version             INTEGER NOT NULL,
    contract_version    INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'draft',
    sheet               JSONB NOT NULL,
    derived             JSONB NOT NULL,
    created_by_email    TEXT NOT NULL,
    submitted_at        TIMESTAMPTZ,
    decided_at          TIMESTAMPTZ,
    decision_comment    TEXT,
    decided_by_email    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT character_sheet_version_status_chk
        CHECK (status IN ('draft', 'submitted', 'approved', 'returned')),
    CONSTRAINT character_sheet_version_version_chk CHECK (version > 0),
    CONSTRAINT character_sheet_version_contract_version_chk
        CHECK (contract_version = 1),
    CONSTRAINT character_sheet_version_character_version_key
        UNIQUE (player_character_id, version),
    CONSTRAINT character_sheet_version_state_chk CHECK (
        (status = 'draft'
            AND submitted_at IS NULL
            AND decided_at IS NULL
            AND decided_by_email IS NULL
            AND decision_comment IS NULL)
        OR (status = 'submitted'
            AND submitted_at IS NOT NULL
            AND decided_at IS NULL
            AND decided_by_email IS NULL
            AND decision_comment IS NULL)
        OR (status = 'approved'
            AND submitted_at IS NOT NULL
            AND decided_at IS NOT NULL
            AND decided_by_email IS NOT NULL)
        OR (status = 'returned'
            AND submitted_at IS NOT NULL
            AND decided_at IS NOT NULL
            AND decided_by_email IS NOT NULL
            AND btrim(decision_comment) <> '')
    )
);

CREATE INDEX character_sheet_version_campaign_character_idx
    ON grimoire.character_sheet_version (campaign_id, player_character_id, version DESC);

CREATE FUNCTION grimoire.enforce_character_sheet_version_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('approved', 'returned') THEN
        RAISE EXCEPTION 'decided character sheet versions are immutable';
    END IF;

    IF NEW.id <> OLD.id
        OR NEW.campaign_id <> OLD.campaign_id
        OR NEW.player_character_id <> OLD.player_character_id
        OR NEW.version <> OLD.version
        OR NEW.contract_version <> OLD.contract_version
        OR NEW.created_by_email <> OLD.created_by_email
        OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'character sheet version identity is immutable';
    END IF;

    IF OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'submitted') THEN
        RAISE EXCEPTION 'invalid character sheet transition';
    END IF;
    IF OLD.status = 'submitted' AND NEW.status NOT IN ('approved', 'returned') THEN
        RAISE EXCEPTION 'invalid character sheet transition';
    END IF;
    IF OLD.status = 'submitted'
        AND (NEW.sheet <> OLD.sheet
            OR NEW.derived <> OLD.derived
            OR NEW.submitted_at <> OLD.submitted_at) THEN
        RAISE EXCEPTION 'submitted character sheet content is immutable';
    END IF;
    IF OLD.status = 'draft' AND NEW.status = 'submitted'
        AND (NEW.sheet <> OLD.sheet OR NEW.derived <> OLD.derived) THEN
        RAISE EXCEPTION 'submit cannot alter character sheet content';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER character_sheet_version_transition
BEFORE UPDATE ON grimoire.character_sheet_version
FOR EACH ROW
EXECUTE FUNCTION grimoire.enforce_character_sheet_version_transition();

CREATE FUNCTION grimoire.prevent_character_sheet_version_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('submitted', 'approved', 'returned') THEN
        RAISE EXCEPTION 'submitted character sheet history cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER character_sheet_version_delete
BEFORE DELETE ON grimoire.character_sheet_version
FOR EACH ROW
EXECUTE FUNCTION grimoire.prevent_character_sheet_version_delete();
