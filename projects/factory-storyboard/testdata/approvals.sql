SET standard_conforming_strings = on;
BEGIN;
DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME';
INSERT INTO public.storyboard_approved_message (itsaplan_project, issue_key, session_id, task_id, text_sha256) VALUES ('HOME', 'HOME-4', 's''1', 't', '11a6767d5674c7e45f7e00dc525762275b3a48491ad6045427d2609cc496c516');
INSERT INTO public.storyboard_approved_message (itsaplan_project, issue_key, session_id, task_id, text_sha256) VALUES ('HOME', 'HOME-4', 's''1', 't', '73473dcc12b763085904a5279d048c4d5b3b008c46f1f32443b99de04aa83a14');
INSERT INTO public.storyboard_approved_message (itsaplan_project, issue_key, session_id, task_id, text_sha256) VALUES ('HOME', 'HOME-5', 's2', 'review', 'fad6057bb5403e59709e79e0679965bd362af5fc0841b06c08f48efa21fc588b');
DO $check$
BEGIN
  IF (SELECT count(*) FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME') <> 3
     OR EXISTS (
      SELECT issue_key, session_id, task_id, text_sha256 FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME'
      EXCEPT VALUES
      ('HOME-4', 's''1', 't', '11a6767d5674c7e45f7e00dc525762275b3a48491ad6045427d2609cc496c516'),
      ('HOME-4', 's''1', 't', '73473dcc12b763085904a5279d048c4d5b3b008c46f1f32443b99de04aa83a14'),
      ('HOME-5', 's2', 'review', 'fad6057bb5403e59709e79e0679965bd362af5fc0841b06c08f48efa21fc588b')) THEN
    RAISE EXCEPTION 'storyboard approvals for % do not match the expected % messages', 'HOME', 3;
  END IF;
  RAISE NOTICE 'storyboard approvals for %: % messages match', 'HOME', 3;
END
$check$;
COMMIT;
