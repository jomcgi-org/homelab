-- Attribution describes the trusted execution path, not a model or a person.
-- NULL historical lanes remain unrecorded; no retrospective attribution.
ALTER TABLE knowledge.review_outcomes
    ADD COLUMN reviewer_lane varchar(32),
    ADD CONSTRAINT review_outcomes_lane_chk CHECK (
        reviewer_lane IS NULL OR reviewer_lane IN ('deterministic-verifier', 'source-review')
    );

CREATE INDEX ix_review_outcomes_history
    ON knowledge.review_outcomes (note_id, attempted_at DESC, id DESC);

-- Join the existing public eligibility boundary. Never expose private reasons,
-- evidence, retry times, prompts, session identifiers or historical claim text.
CREATE VIEW public_api.knowledge_review_history AS
    SELECT r.id, r.note_id, r.status, r.attempted_at, r.reviewer_lane,
           r.note_revision, (r.note_revision = n.revision) AS is_current_revision
    FROM public_api.knowledge_notes AS p
    JOIN knowledge.notes AS n ON n.note_id = p.note_id
    JOIN knowledge.review_outcomes AS r ON r.note_id = p.note_id;
GRANT SELECT ON public_api.knowledge_review_history TO public_reader;
