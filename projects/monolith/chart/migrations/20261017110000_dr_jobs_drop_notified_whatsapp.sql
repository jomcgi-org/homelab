-- Staged follow-up to gateway retirement (#6988). Before production application,
-- an authorized operator must clear live dependencies and verify that deployed
-- vacancy consumers no longer select this column. Keep the shared table, rows
-- and every other notification-state column intact; refuse dependent objects.
ALTER TABLE dr_jobs.nhs_vacancies
    DROP COLUMN notified_whatsapp RESTRICT;
