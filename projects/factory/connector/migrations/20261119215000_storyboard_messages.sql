-- The storyboard publishes session prose by default, without owner approval
-- (HOME-518, decision:
-- https://factory-plan.jomcgi.dev/factory/issue/HOME-414#comment-5459).
--
-- This migration only adds. The publisher of chart 0.1.10 reads
-- storyboard_tasks and storyboard_task_messages, so those views, the
-- storyboard_approved_message table and their grants stay through the
-- rollback window: lowering the storyboard chart pin restores that
-- publisher. A later migration drops them after the new publisher is live
-- (follow-up to HOME-518).

-- The text of each assistant message in a completed task of project HOME.
-- The view fixes the project, so the publisher login never reads the
-- transcripts of another project. It lists a task only if the HOME binding's
-- own kagent Agent ran it. The security agent runs in namespace
-- factory-intsec (HOME-235, HOME-86), and the view also excludes that
-- namespace by name. The connector store holds no issue labels: the
-- publisher reads them from itsaplan and skips private and security issues.
--
-- A task matches its attempt's session, not the work item's current session:
-- when the connector releases the session of a done or stopped work item, it
-- keeps the attempt and its journal. The text is the message as the itsaplan
-- run trace shows it: the content deltas of one message ID joined in order,
-- with U+0000 replaced by U+FFFD. Artifact text is excluded. A review round
-- has no journal, so it has no rows.
CREATE VIEW public.storyboard_messages WITH (security_barrier = true) AS
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
        chr(1), '\\')::json -> 'events' AS list
) AS parsed
-- A journal without text events stores "events": null.
CROSS JOIN LATERAL json_array_elements(
    CASE json_typeof(parsed.list) WHEN 'array' THEN parsed.list ELSE '[]' END
) WITH ORDINALITY AS events(event, n)
WHERE binding.itsaplan_project = 'HOME'
  AND events.event ->> 'type' = 'TEXT_MESSAGE_CONTENT'
  AND events.event ->> 'messageId' NOT LIKE 'artifact/%'
GROUP BY binding.itsaplan_project, item.issue_id, task.session_id, journal.task_id,
         task.finished_at, journal.run_id, journal.attempt, events.event ->> 'messageId';

REVOKE ALL ON public.storyboard_messages FROM PUBLIC;
GRANT SELECT ON public.storyboard_messages TO factory_storyboard;
