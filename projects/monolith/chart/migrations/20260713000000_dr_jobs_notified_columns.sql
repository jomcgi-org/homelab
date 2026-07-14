-- dr-jobs notification state moves onto the vacancy row itself, so "was this
-- posting announced?" becomes a column check rather than cross-system
-- archaeology (scrape log -> outbox retention -> channel history). One nullable
-- timestamp per channel: NULL means pending, a set value means delivered. A
-- failed enqueue leaves the column NULL and the next hourly scrape retries
-- (self-healing), replacing the old fire-and-forget notify() path that failed
-- silently in the DATABASE_URL-only Argo job pod.
ALTER TABLE dr_jobs.nhs_vacancies
    ADD COLUMN notified_discord  timestamptz,
    ADD COLUMN notified_whatsapp timestamptz;

-- Backfill every existing row as already-notified so the switchover does not
-- dump the current backlog into the chat (the data-model equivalent of the old
-- seed-run digest suppression). Only postings scraped after this migration keep
-- NULL columns and are therefore announced.
UPDATE dr_jobs.nhs_vacancies
    SET notified_discord = now(),
        notified_whatsapp = now();
