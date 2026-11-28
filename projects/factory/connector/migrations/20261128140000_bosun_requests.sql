-- Bosun's factory requests (HOME-941). bosun-requests inserts one row per
-- Discord turn in which Joe asks for factory work; the connector files the
-- HOME issue and follows it to Done or Canceled; the Discord bridge then
-- replies to the origin message once, through bosun-requests, which sets
-- replied_at. The Discord IDs come from the bridge's record of the turn,
-- never from the model.
CREATE TABLE bosun_request (
    id bigserial PRIMARY KEY,
    guild_id text NOT NULL CHECK (guild_id ~ '^[0-9]{1,20}$'),
    channel_id text NOT NULL CHECK (channel_id ~ '^[0-9]{1,20}$'),
    message_id text NOT NULL UNIQUE CHECK (message_id ~ '^[0-9]{1,20}$'),
    author_id text NOT NULL CHECK (author_id ~ '^[0-9]{1,20}$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 4000),
    created_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'filed', 'done', 'canceled', 'failed')),
    issue_id bigint UNIQUE,
    issue_key text,
    column_name text,
    tries integer NOT NULL DEFAULT 0,
    error text,
    filed_at timestamptz,
    finished_at timestamptz,
    replied_at timestamptz,
    next_check_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'pending') = (issue_id IS NULL) OR state = 'failed'),
    -- Only a finished request is answered.
    CHECK (replied_at IS NULL OR state IN ('done', 'canceled'))
);
CREATE INDEX bosun_request_open ON bosun_request (next_check_at)
    WHERE state IN ('pending', 'filed');
CREATE INDEX bosun_request_unreplied ON bosun_request (id)
    WHERE state IN ('done', 'canceled') AND replied_at IS NULL;

-- What request_status shows: the request, its issue and the issue's PR.
CREATE VIEW public.bosun_request_status AS
SELECT r.id, r.guild_id, r.channel_id, r.message_id, r.title, r.state,
       r.issue_key, r.column_name, r.created_at, r.filed_at, r.finished_at,
       r.replied_at, item.state AS work_state, pr.number AS pr_number,
       pr.state AS pr_state, pr.merged_at
FROM public.bosun_request AS r
LEFT JOIN public.work_item AS item ON item.issue_id = r.issue_id
LEFT JOIN public.pull_request AS pr ON pr.work_item_id = item.id;

-- bosun-requests may add a request, read status and mark a reply sent,
-- nothing else.
REVOKE ALL ON public.bosun_request, public.bosun_request_status FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO factory_bosun_requests;
GRANT INSERT (guild_id, channel_id, message_id, author_id, title, description)
    ON public.bosun_request TO factory_bosun_requests;
-- RETURNING id, the lookup by message and the reply update read these.
GRANT SELECT (id, message_id, replied_at) ON public.bosun_request TO factory_bosun_requests;
GRANT UPDATE (replied_at) ON public.bosun_request TO factory_bosun_requests;
GRANT USAGE ON SEQUENCE public.bosun_request_id_seq TO factory_bosun_requests;
GRANT SELECT ON public.bosun_request_status TO factory_bosun_requests;
