CREATE SCHEMA IF NOT EXISTS updates;

CREATE TABLE updates.product_update (
    published_on DATE PRIMARY KEY,
    category TEXT NOT NULL
        CHECK (category IN ('new-feature', 'improvement', 'fix')),
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    highlights JSONB NOT NULL,
    improvements JSONB NOT NULL DEFAULT '[]'::jsonb,
    projects JSONB NOT NULL,
    technologies JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_base_sha TEXT NOT NULL
        CHECK (source_base_sha ~ '^[0-9a-f]{40}$'),
    source_head_sha TEXT NOT NULL,
    source_commit_count INTEGER NOT NULL
        CHECK (source_commit_count BETWEEN 1 AND 1000),
    submitted_by TEXT NOT NULL,
    submitted_authority TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT product_update_head_sha_key UNIQUE (source_head_sha),
    CONSTRAINT product_update_source_head_sha_check
        CHECK (source_head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT product_update_source_range_check
        CHECK (source_base_sha <> source_head_sha),
    CONSTRAINT product_update_highlights_array_check
        CHECK (jsonb_typeof(highlights) = 'array'),
    CONSTRAINT product_update_improvements_array_check
        CHECK (jsonb_typeof(improvements) = 'array'),
    CONSTRAINT product_update_projects_array_check
        CHECK (jsonb_typeof(projects) = 'array'),
    CONSTRAINT product_update_technologies_array_check
        CHECK (jsonb_typeof(technologies) = 'array')
);

COMMENT ON TABLE updates.product_update IS
    'Private, immediately visible daily product-update journal entries submitted through MCP.';
