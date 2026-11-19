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
  AND attempt.context_id = item.session_id
  AND attempt.task_id IS NOT NULL;

REVOKE ALL ON public.storyboard_tasks FROM PUBLIC;
GRANT SELECT ON public.storyboard_tasks TO factory_storyboard;
