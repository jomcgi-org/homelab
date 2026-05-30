-- knowledge.gaps: enforce valid (state, gap_class) combinations.
--
-- Without this CHECK, two independent constraints (gaps_state_check and
-- gaps_gap_class_check) admit any cross-product of state x gap_class,
-- including combinations the lifecycle should never produce. Tonight's
-- audit found 231 rows in (state='classified', gap_class='external') --
-- per the classifier docs, external/internal/hybrid gaps move to
-- in_review after classification; only parked gaps go to 'classified'
-- terminal state. Those 231 rows were a historical drift, cleaned up
-- in the bulk gap re-classification pass.
--
-- This constraint prevents recurrence: any UPDATE/INSERT that produces
-- an invalid combo will now fail loudly with a CHECK violation.
--
-- 'discovered' is treated as wildcard because the classifier's frontmatter
-- edits to gap_class and state are sequential text replacements, not a
-- single atomic update. There is a brief window where the file has
-- gap_class set but state still 'discovered' before the second edit
-- lands; the reconciler may persist that intermediate state. Allowing
-- (discovered, *) tolerates the race.
--
-- SQLite ignores CHECK constraints, so unit-test fixtures will not
-- exercise this. Real Postgres in CI and prod will.
ALTER TABLE knowledge.gaps DROP CONSTRAINT IF EXISTS gaps_state_class_combo;
ALTER TABLE knowledge.gaps ADD CONSTRAINT gaps_state_class_combo CHECK (
  state = 'discovered'
  OR (state = 'in_review' AND gap_class IN ('external', 'internal', 'hybrid'))
  OR (state IN ('researching', 'researched', 'verified', 'consolidated')
      AND gap_class = 'external')
  OR (state IN ('committed', 'rejected')
      AND gap_class IN ('external', 'internal', 'hybrid'))
  OR (state = 'parked' AND gap_class = 'parked')
  OR (state = 'classified' AND gap_class = 'parked')
);
