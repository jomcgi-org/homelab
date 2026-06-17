-- dr_jobs schema: NHS Scotland (apply.jobs.scot.nhs.uk) vacancy aggregator.
--
-- nhs_vacancies : one row per JobTrain vacancy, keyed by the numeric JobId.
--   The daily scrape (dr_jobs.scrape_nhs) upserts the structured JSON-LD
--   fields and stamps last_seen_at. Rows are never deleted: the live view
--   filters on last_seen_at + closing_date, the history view shows the rest.
--   The filter scope (keyword + salary bands) lives in the scraper, not here.

CREATE SCHEMA IF NOT EXISTS dr_jobs;

CREATE TABLE dr_jobs.nhs_vacancies (
    job_id              TEXT PRIMARY KEY,
    reference           TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL,
    employment_type     TEXT NOT NULL DEFAULT '',
    salary_band         TEXT NOT NULL DEFAULT '',
    salary_text         TEXT NOT NULL DEFAULT '',
    town                TEXT NOT NULL DEFAULT '',
    postcode            TEXT NOT NULL DEFAULT '',
    region              TEXT NOT NULL DEFAULT '',
    posted_date         DATE,
    closing_date        DATE,
    url                 TEXT NOT NULL,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_nhs_vacancies_closing ON dr_jobs.nhs_vacancies (closing_date);
CREATE INDEX idx_nhs_vacancies_last_seen ON dr_jobs.nhs_vacancies (last_seen_at DESC);
