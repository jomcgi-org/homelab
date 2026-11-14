-- Keep the queue's synthetic commit after its entry disappears so an
-- ejection's revision round can read the checks that failed on it.
ALTER TABLE pull_request ADD COLUMN merge_group_sha text;
