-- HOME-727. Older attempts have no verified analysis commit.
ALTER TABLE delivery_attempt
    ADD COLUMN analysis_main_sha text CHECK (analysis_main_sha ~ '^[0-9a-f]{40}$');
