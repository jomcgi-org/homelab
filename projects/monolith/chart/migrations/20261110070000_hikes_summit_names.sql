-- Nullable: old scrapes have no explicit summit evidence.
ALTER TABLE hikes.walks ADD COLUMN summit_names JSONB;
