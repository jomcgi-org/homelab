/* invariant: FACT-13 */
-- Keep the merged head until its PR-specific review has finished.
CREATE TABLE pending_merge_review (
    binding_name text NOT NULL REFERENCES repo_binding(name),
    number integer NOT NULL,
    head_sha text NOT NULL,
    pr_url text NOT NULL,
    next_poll timestamptz NOT NULL DEFAULT '-infinity',
    attempts integer NOT NULL DEFAULT 0,
    PRIMARY KEY (binding_name, number)
);
CREATE INDEX pending_merge_review_due ON pending_merge_review(binding_name, next_poll);
