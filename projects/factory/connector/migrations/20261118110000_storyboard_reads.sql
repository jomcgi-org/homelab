-- The storyboard joins these delivery facts to itsaplan issues after checking
-- project membership and labels. Raw prompts and transcripts stay private.
CREATE VIEW public.storyboard_delivery AS
SELECT binding.itsaplan_project, item.issue_id, item.issue_key, item.session_id,
       item.state, item.review_rounds, pr.number AS pr_number, pr.merged_at
FROM public.work_item AS item
JOIN public.repo_binding AS binding ON binding.name = item.binding_name
LEFT JOIN public.pull_request AS pr ON pr.work_item_id = item.id;

REVOKE ALL ON public.storyboard_delivery FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO factory_storyboard;
GRANT SELECT ON public.storyboard_delivery TO factory_storyboard;
