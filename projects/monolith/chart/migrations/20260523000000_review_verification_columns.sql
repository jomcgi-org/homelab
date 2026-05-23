-- Add human-verification flags so the /private/review audit page can
-- distinguish automation-made decisions from human-confirmed ones.
--
-- Both columns default to FALSE so historical/pre-existing rows show up
-- in the audit queue until a human spot-checks them. This is intentional:
-- the review surface bootstraps from "everything needs review" rather
-- than "nothing needs review".
--
-- Mirrors the SQLModel columns Gap.human_verified and
-- Note.visibility_verified in projects/monolith/knowledge/models.py.

ALTER TABLE knowledge.gaps
    ADD COLUMN human_verified boolean NOT NULL DEFAULT false;

ALTER TABLE knowledge.notes
    ADD COLUMN visibility_verified boolean NOT NULL DEFAULT false;
