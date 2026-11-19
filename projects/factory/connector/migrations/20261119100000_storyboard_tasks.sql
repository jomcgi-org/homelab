ALTER TABLE public.delivery_attempt ADD COLUMN kagent_outcome text;

-- Only expose completed tasks for the work item's current session. The
-- publisher checks the project and issue labels before requesting a task.
CREATE VIEW public.storyboard_tasks AS
SELECT DISTINCT binding.itsaplan_project, item.issue_id, item.session_id,
       attempt.kagent_namespace, attempt.kagent_agent, attempt.task_id
FROM public.work_item AS item
JOIN public.repo_binding AS binding ON binding.name = item.binding_name
JOIN public.run AS run ON run.work_item_id = item.id
JOIN public.delivery_attempt AS attempt ON attempt.run_id = run.id
WHERE attempt.state = 'completed'
  AND attempt.kagent_outcome = 'TASK_STATE_COMPLETED'
  AND attempt.context_id = item.session_id
  AND attempt.task_id IS NOT NULL;

REVOKE ALL ON public.storyboard_tasks FROM PUBLIC;
GRANT SELECT ON public.storyboard_tasks TO factory_storyboard;

-- The owner's publication approvals. Each row approves one assistant message
-- by the SHA-256 of its text, in one task of one issue in one project. Only
-- the table owner writes it (projects/factory-storyboard/approvals.py), and
-- factory_storyboard gets no grant.
CREATE TABLE public.storyboard_approved_message (
    itsaplan_project text NOT NULL,
    issue_key text NOT NULL,
    session_id text NOT NULL,
    task_id text NOT NULL,
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (itsaplan_project, issue_key, session_id, task_id, text_sha256)
);

REVOKE ALL ON public.storyboard_approved_message FROM PUBLIC;

-- The approved digests that occur in a completed task of the current
-- session. The connector journals kagent task history in task_transcript;
-- the text stays private. A digest covers the message as the itsaplan run
-- trace shows it: the text parts joined by newlines, with U+0000 replaced by
-- U+FFFD. Artifact text is excluded. The view returns only digests that the
-- owner approved, so the publisher learns nothing about other messages. The
-- publisher therefore needs no kagent access.
CREATE VIEW public.storyboard_task_messages WITH (security_barrier = true) AS
SELECT DISTINCT task.itsaplan_project, task.issue_id, task.session_id, task.task_id,
       task.text_sha256
FROM (
    SELECT binding.itsaplan_project, item.issue_id, item.issue_key, item.session_id,
           attempt.task_id,
           encode(sha256(convert_to(string_agg(event ->> 'delta', '' ORDER BY n),
               'UTF8')), 'hex') AS text_sha256
    FROM public.work_item AS item
    JOIN public.repo_binding AS binding ON binding.name = item.binding_name
    JOIN public.run AS run ON run.work_item_id = item.id
    JOIN public.delivery_attempt AS attempt ON attempt.run_id = run.id
    JOIN public.task_transcript AS journal
        ON journal.run_id = attempt.run_id AND journal.task_id = attempt.task_id
    -- JSON operators cannot decode \u0000. Escaped backslash pairs move to
    -- chr(1), which valid JSON never holds unescaped. Each remaining \u0000
    -- becomes a literal U+FFFD, which also decodes in a SQL_ASCII database.
    CROSS JOIN LATERAL (
        SELECT replace(replace(replace(convert_from(journal.state, 'UTF8'),
            '\\', chr(1)), '\u0000', convert_from('\xefbfbd', 'UTF8')),
            chr(1), '\\')::json -> 'events' AS list
    ) AS parsed
    -- A journal without text events stores "events": null.
    CROSS JOIN LATERAL json_array_elements(
        CASE json_typeof(parsed.list) WHEN 'array' THEN parsed.list ELSE '[]' END
    ) WITH ORDINALITY AS events(event, n)
    WHERE attempt.state = 'completed'
      AND attempt.kagent_outcome = 'TASK_STATE_COMPLETED'
      AND attempt.context_id = item.session_id
      AND event ->> 'type' = 'TEXT_MESSAGE_CONTENT'
      AND event ->> 'messageId' NOT LIKE 'artifact/%'
      -- Parse only the journals of tasks with an approved message.
      AND EXISTS (
          SELECT 1 FROM public.storyboard_approved_message AS approved
          WHERE approved.itsaplan_project = binding.itsaplan_project
            AND approved.issue_key = item.issue_key
            AND approved.session_id = item.session_id
            AND approved.task_id = attempt.task_id)
    GROUP BY binding.itsaplan_project, item.issue_id, item.issue_key, item.session_id,
             attempt.task_id, journal.run_id, journal.attempt, event ->> 'messageId'
) AS task
JOIN public.storyboard_approved_message AS approved
    ON approved.itsaplan_project = task.itsaplan_project
   AND approved.issue_key = task.issue_key
   AND approved.session_id = task.session_id
   AND approved.task_id = task.task_id
   AND approved.text_sha256 = task.text_sha256;

REVOKE ALL ON public.storyboard_task_messages FROM PUBLIC;
GRANT SELECT ON public.storyboard_task_messages TO factory_storyboard;
