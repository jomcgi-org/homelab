ALTER TABLE work_item
    ADD COLUMN first_arm text NOT NULL DEFAULT 'control',
    ADD COLUMN ci_fix_rounds integer NOT NULL DEFAULT 0,
    ADD COLUMN escalated boolean NOT NULL DEFAULT false;
