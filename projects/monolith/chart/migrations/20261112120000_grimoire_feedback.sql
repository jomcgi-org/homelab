-- In-app feedback from the friends web app, held for operator triage.
-- Private: no public_reader grant. Only the title and body an operator writes
-- at triage time are published as a GitHub issue; this row never is.
CREATE TABLE grimoire.feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    author_email TEXT NOT NULL,
    campaign_id UUID REFERENCES grimoire.campaign(id) ON DELETE SET NULL,
    page TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    issue_number INTEGER,
    resolved_at TIMESTAMPTZ,
    CONSTRAINT feedback_status_chk CHECK (status IN ('new', 'filed', 'dismissed')),
    CONSTRAINT feedback_text_chk CHECK (length(text) BETWEEN 1 AND 2000),
    CONSTRAINT feedback_page_chk CHECK (length(page) <= 200),
    CONSTRAINT feedback_issue_chk CHECK ((status = 'filed') = (issue_number IS NOT NULL)),
    CONSTRAINT feedback_resolved_chk CHECK ((status = 'new') = (resolved_at IS NULL))
);
CREATE INDEX feedback_status_created_idx ON grimoire.feedback (status, created_at);
CREATE INDEX feedback_author_created_idx ON grimoire.feedback (author_email, created_at);
