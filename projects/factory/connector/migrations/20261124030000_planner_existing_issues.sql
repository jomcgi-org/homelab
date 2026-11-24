-- Stable identities for the existing scheduled-agent proposals. Use the HOME
-- identifier as the object so renaming or closing an issue preserves the match.
CREATE TABLE planner_existing_issue (
    issue_key text NOT NULL,
    fingerprint text NOT NULL UNIQUE
);
INSERT INTO planner_existing_issue (issue_key, fingerprint)
SELECT 'HOME-' || n, 'homelab:' || area || ':home-' || n || ':scheduled-agent'
FROM generate_series(4, 11) AS n
CROSS JOIN (VALUES ('delivery'), ('trust'), ('docs')) AS pilot(area);

-- Fingerprint searches precede reservation. Keep their journal tied to the run.
ALTER TABLE planner_tracker_attempt DROP CONSTRAINT planner_tracker_attempt_run_id_fingerprint_fkey,
    ADD FOREIGN KEY (run_id) REFERENCES planner_run;
ALTER TABLE planner_run ADD COLUMN lookup_after timestamptz NOT NULL DEFAULT now();
