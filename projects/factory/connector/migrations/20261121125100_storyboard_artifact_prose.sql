-- Publish completed Codex prose segments as well as conversation messages.
-- Joe's decision: https://factory-plan.jomcgi.dev/factory/issue/HOME-414#comment-5512
-- The connector records prose as text and tool calls and results as data.
-- A hashed message ID must match a closed cursor in this exact task journal.
-- The project, agent, finished-task and role boundaries stay on this view.
CREATE OR REPLACE VIEW public.storyboard_messages WITH (security_barrier = true) AS
SELECT binding.itsaplan_project, item.issue_id, task.session_id, journal.task_id,
       task.finished_at, journal.attempt AS journal_attempt,
       min(events.n) AS first_event,
       string_agg(events.event ->> 'delta', '' ORDER BY events.n) AS text
FROM public.repo_binding AS binding
JOIN public.work_item AS item ON item.binding_name = binding.name
JOIN public.run AS run ON run.work_item_id = item.id
JOIN public.task_transcript AS journal ON journal.run_id = run.id
-- One completed attempt of the task. kagent keeps a task in one session.
CROSS JOIN LATERAL (
    SELECT attempt.context_id AS session_id, attempt.finished_at
    FROM public.delivery_attempt AS attempt
    WHERE attempt.run_id = journal.run_id
      AND attempt.task_id = journal.task_id
      AND attempt.context_id IS NOT NULL
      AND attempt.state = 'completed'
      AND attempt.kagent_outcome = 'TASK_STATE_COMPLETED'
      AND attempt.finished_at IS NOT NULL
      AND attempt.kagent_namespace = binding.kagent_namespace
      AND attempt.kagent_agent = binding.kagent_agent
      AND attempt.kagent_namespace <> 'factory-intsec'
    ORDER BY attempt.finished_at, attempt.id
    LIMIT 1
) AS task
-- JSON operators cannot decode \u0000. Escaped backslash pairs move to
-- chr(1), which valid JSON never holds unescaped. Each remaining \u0000
-- becomes a literal U+FFFD, which also decodes in a SQL_ASCII database.
CROSS JOIN LATERAL (
    SELECT replace(replace(replace(convert_from(journal.state, 'UTF8'),
        '\\', chr(1)), '\u0000', convert_from('\xefbfbd', 'UTF8')),
        chr(1), '\\')::json AS state
) AS parsed
-- A journal without text events stores "events": null.
CROSS JOIN LATERAL json_array_elements(
    CASE json_typeof(parsed.state -> 'events') WHEN 'array' THEN parsed.state -> 'events' ELSE '[]' END
) WITH ORDINALITY AS events(event, n)
WHERE binding.itsaplan_project = 'HOME'
  AND events.event ->> 'type' = 'TEXT_MESSAGE_CONTENT'
  AND (
      events.event ->> 'messageId' NOT LIKE 'artifact/%'
      OR EXISTS (
          SELECT 1 FROM json_array_elements(
              CASE json_typeof(parsed.state -> 'artifacts')
                  WHEN 'array' THEN parsed.state -> 'artifacts'
                  ELSE '[]'
              END
          ) AS artifact(cursor)
          WHERE artifact.cursor ->> 'closed' = 'true'
            AND artifact.cursor ->> 'id' <> ''
            AND events.event ->> 'messageId' = 'artifact/' || encode(
                sha256(convert_to(journal.task_id, 'UTF8') || '\x00'::bytea ||
                    convert_to(artifact.cursor ->> 'id', 'UTF8')),
                'hex'
            )
      )
  )
GROUP BY binding.itsaplan_project, item.issue_id, task.session_id, journal.task_id,
         task.finished_at, journal.run_id, journal.attempt, events.event ->> 'messageId';
