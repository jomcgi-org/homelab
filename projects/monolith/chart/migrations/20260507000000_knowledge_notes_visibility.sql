-- Add visibility label so notes can be selectively exposed via the
-- /api/knowledge/public/* surface. NULL is treated as private at
-- serving time; an explicit value is required to expose a note.
ALTER TABLE knowledge.notes
  ADD COLUMN visibility text NULL;

ALTER TABLE knowledge.notes
  ADD CONSTRAINT notes_visibility_chk
  CHECK (visibility IS NULL OR visibility IN ('public', 'private'));

-- Partial index: public set is the small, hot read path; private/null
-- reads do not need the index.
CREATE INDEX notes_visibility_idx
  ON knowledge.notes (visibility)
  WHERE visibility = 'public';
