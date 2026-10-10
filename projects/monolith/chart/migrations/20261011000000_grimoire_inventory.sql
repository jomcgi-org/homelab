CREATE TABLE grimoire.inventory_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('party', 'character')),
    player_character_id UUID REFERENCES grimoire.player_character(id) ON DELETE CASCADE,
    CHECK ((owner_kind = 'party') = (player_character_id IS NULL)),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    entity_id UUID REFERENCES grimoire.entity(id) ON DELETE SET NULL,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 0 AND 1000000),
    notes TEXT NOT NULL DEFAULT '',
    hidden_from_party BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX inventory_item_campaign_live_idx
    ON grimoire.inventory_item (campaign_id, owner_kind, player_character_id)
    WHERE deleted_at IS NULL;

CREATE TABLE grimoire.inventory_change (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES grimoire.campaign(id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES grimoire.inventory_item(id) ON DELETE CASCADE,
    who_member_id UUID REFERENCES grimoire.campaign_member(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'move', 'delete')),
    delta INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL CHECK (quantity_after BETWEEN 0 AND 1000000),
    reason TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 500),
    changes JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(changes) = 'object'),
    session_id UUID REFERENCES grimoire.game_session(id) ON DELETE SET NULL,
    event_id UUID REFERENCES grimoire.session_event(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX inventory_change_campaign_item_idx
    ON grimoire.inventory_change (campaign_id, item_id, created_at);

CREATE FUNCTION grimoire.inventory_change_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- FK SET NULL actions are nested triggers, not audit edits. Allow only
    -- those three provenance columns to become NULL; direct updates fail.
    IF pg_trigger_depth() > 1
       AND (to_jsonb(NEW) - ARRAY['who_member_id', 'session_id', 'event_id'])
         = (to_jsonb(OLD) - ARRAY['who_member_id', 'session_id', 'event_id'])
       AND (NEW.who_member_id IS NULL OR NEW.who_member_id IS NOT DISTINCT FROM OLD.who_member_id)
       AND (NEW.session_id IS NULL OR NEW.session_id IS NOT DISTINCT FROM OLD.session_id)
       AND (NEW.event_id IS NULL OR NEW.event_id IS NOT DISTINCT FROM OLD.event_id)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'inventory_change is append-only';
END;
$$;
CREATE TRIGGER inventory_change_append_only
    BEFORE UPDATE ON grimoire.inventory_change
    FOR EACH ROW EXECUTE FUNCTION grimoire.inventory_change_append_only();

CREATE FUNCTION grimoire.inventory_change_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- FK cascades from campaign and inventory_item cleanup run as nested
    -- triggers, so only those may delete audit rows. Direct deletes fail.
    IF pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'inventory_change is append-only';
END;
$$;
CREATE TRIGGER inventory_change_no_delete
    BEFORE DELETE ON grimoire.inventory_change
    FOR EACH ROW EXECUTE FUNCTION grimoire.inventory_change_no_delete();

CREATE FUNCTION grimoire.inventory_item_no_hard_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Items are soft-deleted by the router, so only FK cascades from
    -- campaign and player_character cleanup may hard-delete them.
    -- Direct deletes fail.
    IF pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'inventory_item is soft-deleted';
END;
$$;
CREATE TRIGGER inventory_item_no_hard_delete
    BEFORE DELETE ON grimoire.inventory_item
    FOR EACH ROW EXECUTE FUNCTION grimoire.inventory_item_no_hard_delete();
