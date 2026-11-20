-- Stable identities for the existing scheduled-agent proposals. Use the HOME
-- identifier as the object so renaming or closing an issue preserves the match.
CREATE TABLE planner_existing_issue (
    issue_key text PRIMARY KEY,
    fingerprint text NOT NULL UNIQUE
);
INSERT INTO planner_existing_issue (issue_key, fingerprint) VALUES
    ('HOME-4', 'homelab:planner:home-4:scheduled-agent'),
    ('HOME-5', 'homelab:planner:home-5:scheduled-agent'),
    ('HOME-6', 'homelab:planner:home-6:scheduled-agent'),
    ('HOME-7', 'homelab:planner:home-7:scheduled-agent'),
    ('HOME-8', 'homelab:planner:home-8:scheduled-agent'),
    ('HOME-9', 'homelab:planner:home-9:scheduled-agent'),
    ('HOME-10', 'homelab:planner:home-10:scheduled-agent'),
    ('HOME-11', 'homelab:planner:home-11:scheduled-agent');
