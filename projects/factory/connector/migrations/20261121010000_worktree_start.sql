ALTER TABLE delivery_attempt
    ADD COLUMN actual_start_sha text CHECK (actual_start_sha ~ '^[0-9a-f]{40}$'),
    ADD COLUMN start_mismatch boolean GENERATED ALWAYS AS (actual_start_sha <> base_sha) STORED;
