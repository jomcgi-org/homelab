-- A transport redelivery retains the original delegation's stop boundary.
-- Existing unresolved runs conservatively retain zero: they cannot override
-- a stop whose ordering relative to their original claim is unknown.
ALTER TABLE run ADD COLUMN admission_stops integer NOT NULL DEFAULT 0;
