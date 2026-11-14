-- Bounded coordinator lookup windows survive writer and engine replacement.
-- run_check belongs to one run attempt; replacement to one delivery attempt.
-- A non-refusal resets the window; closing the run removes both kinds.
CREATE TABLE coordinator_auth_failure (
    operation text NOT NULL CHECK (operation IN ('run_check', 'replacement')),
    owner_id bigint NOT NULL,
    attempt integer NOT NULL CHECK (
        (operation = 'run_check' AND attempt >= 1) OR
        (operation = 'replacement' AND attempt = 0)),
    first_at timestamptz NOT NULL,
    PRIMARY KEY (operation, owner_id, attempt)
);
