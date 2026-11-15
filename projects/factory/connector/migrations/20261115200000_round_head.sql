-- The head a review round was asked to change. A round that closes with the
-- PR still at this head pushed nothing, so no new check or review can start
-- another round; the connector reports it once on the issue.
ALTER TABLE run ADD COLUMN round_head_sha text;
