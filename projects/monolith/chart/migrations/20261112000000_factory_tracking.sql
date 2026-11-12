-- Private information recording (#7400). Existing work remains executable.
ALTER TABLE swarm.work_item
    ADD COLUMN execution_mode text NOT NULL DEFAULT 'factory',
    ADD COLUMN tracking_kind text,
    ADD COLUMN owner text,
    ADD COLUMN blocker text NOT NULL DEFAULT '',
    ADD COLUMN next_action text NOT NULL DEFAULT '',
    ADD COLUMN completion_evidence text,
    ADD COLUMN completed_at timestamptz,
    ADD COLUMN tracking_version integer NOT NULL DEFAULT 0,
    ADD CONSTRAINT work_item_execution_mode_check
        CHECK (execution_mode IN ('factory','tracking_only')),
    ADD CONSTRAINT work_item_tracking_check CHECK (
        execution_mode = 'factory' OR (
            authority = 'local' AND source_kind = 'mcp' AND source_ref IS NOT NULL
            AND github_repo IS NULL AND github_issue_number IS NULL
            AND github_pointer_comment_id IS NULL
            AND tracking_kind IS NOT NULL AND tracking_kind IN ('manual','external')
            AND owner IS NOT NULL AND length(owner) BETWEEN 1 AND 256
            AND tracking_version >= 1
            AND (state <> 'done' OR (completion_evidence IS NOT NULL
                AND length(completion_evidence) > 0 AND completed_at IS NOT NULL))
        )
    );

CREATE UNIQUE INDEX work_item_tracking_source_key ON swarm.work_item (source_ref)
    WHERE execution_mode = 'tracking_only';

CREATE TABLE swarm.tracking_request (
    id bigserial PRIMARY KEY,
    actor text NOT NULL,
    request_key text NOT NULL,
    payload_json text NOT NULL,
    result_json text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tracking_request_key UNIQUE (actor, request_key)
);

-- Database protection covers writes outside the private recording API too.
CREATE FUNCTION swarm.guard_tracking_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.execution_mode = 'tracking_only' THEN
            RAISE EXCEPTION 'tracking records cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.execution_mode IS DISTINCT FROM OLD.execution_mode THEN
        RAISE EXCEPTION 'work item execution mode is immutable';
    END IF;
    IF OLD.execution_mode = 'tracking_only' AND (
        NEW.source_ref IS DISTINCT FROM OLD.source_ref OR
        NEW.source_kind IS DISTINCT FROM OLD.source_kind OR
        NEW.tracking_kind IS DISTINCT FROM OLD.tracking_kind OR
        NEW.owner IS DISTINCT FROM OLD.owner OR
        NEW.authority IS DISTINCT FROM OLD.authority
    ) THEN
        RAISE EXCEPTION 'tracking identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER work_item_tracking_identity
    BEFORE UPDATE OR DELETE ON swarm.work_item
    FOR EACH ROW EXECUTE FUNCTION swarm.guard_tracking_identity();

CREATE FUNCTION swarm.guard_receipt_work_item() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.work_item_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM swarm.work_item w WHERE w.id = NEW.work_item_id
        AND w.execution_mode = 'factory'
    ) THEN
        RAISE EXCEPTION 'receipt work item is non-executable or missing';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER factory_receipt_executable_work
    BEFORE INSERT OR UPDATE ON swarm.factory_receipt
    FOR EACH ROW EXECUTE FUNCTION swarm.guard_receipt_work_item();

-- No public_reader grant: tracking_request and tracking fields stay private.
