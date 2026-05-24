-- Gate external research behind the review queue.
--
-- Pairs with the classifier-prompt change (CLASSIFIER_VERSION opus-4-7@v2)
-- that routes external -> in_review instead of straight to classified.
-- This migration retroactively gates the ~210 external+classified rows
-- already queued for the daily research cron, so they appear in the
-- pending review-queue UI and only drain after explicit approval via
-- POST /api/knowledge/gaps/{id}/approve (which flips in_review back to
-- classified, where the existing _sweep_and_select_candidates picks them
-- up unchanged).
--
-- Idempotent: the narrow WHERE clause makes re-applying a no-op once the
-- new classifier is the only writer of external rows, because the new
-- classifier never produces external+classified.
--
-- No CHECK-constraint change needed: 'in_review' has been a valid state
-- since 20260425040000_knowledge_gaps_state_check_widen.sql.
UPDATE knowledge.gaps
   SET state = 'in_review'
 WHERE deleted_at IS NULL
   AND gap_class  = 'external'
   AND state      = 'classified';
