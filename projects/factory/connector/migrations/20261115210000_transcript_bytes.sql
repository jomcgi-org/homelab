-- JSONB rejects escaped U+0000. Keep the journal's JSON as bytes so valid
-- source text can be replayed unchanged, including after a lost acknowledgement.
ALTER TABLE task_transcript
    ALTER COLUMN state TYPE bytea USING convert_to(state::text, 'UTF8');
