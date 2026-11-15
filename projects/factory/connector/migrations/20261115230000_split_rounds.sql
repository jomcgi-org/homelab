-- A PR over the reviewer's size budget is not a review failure. Persist the
-- separate split-round bound, as rebase_rounds does for conflicts.
ALTER TABLE work_item
    ADD COLUMN split_rounds integer NOT NULL DEFAULT 0 CHECK (split_rounds >= 0);
