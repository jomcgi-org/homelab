-- A work item's execution belongs to one kagent caller, including after a
-- restart or a replacement session. NULL is legacy, not permission to rebind:
-- dispatch requires positive ownership evidence before filling it for work
-- that has already sent input. Contains identity metadata, never token bytes.
ALTER TABLE work_item ADD COLUMN caller_identity text;
