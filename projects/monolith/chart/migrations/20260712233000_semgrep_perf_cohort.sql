-- Cohort metadata for the Route B vs Semgrep Managed perf comparison: the shape
-- of the scanned diff (changed-file count, changed lines, per-language
-- breakdown) so the dashboard can segment speedup by cohort and show which diff
-- shapes are at parity vs a major speedup. Populated on route-b rows (a matched
-- pair inherits the cohort by commit_sha), computed live from the PR's
-- /pulls/{n}/files stats and backfilled for historical rows via the GitHub API.
-- Nullable: existing rows stay NULL until backfilled.
ALTER TABLE semgrep.scan_perf
    ADD COLUMN IF NOT EXISTS file_count    INTEGER,
    ADD COLUMN IF NOT EXISTS changed_lines INTEGER,
    ADD COLUMN IF NOT EXISTS languages     JSONB;
