ALTER TABLE repo_binding ADD COLUMN review_merges_since timestamptz NOT NULL DEFAULT date_trunc('second', now());
