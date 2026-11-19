SET standard_conforming_strings = on;
BEGIN;
DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME';
DO $check$
BEGIN
  IF (SELECT count(*) FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME') <> 0 THEN
    RAISE EXCEPTION 'storyboard approvals for % do not match the expected % messages', 'HOME', 0;
  END IF;
  RAISE NOTICE 'storyboard approvals for %: % messages match', 'HOME', 0;
END
$check$;
COMMIT;
