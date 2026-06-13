-- Stars ERA5 climatology backfill (ADR 009): a long-run seasonal baseline that
-- pre-fills the historical heatmap before the live accumulator (site_month_stats,
-- ADR 008) has banked enough elapsed forecast hours to be meaningful.
--
-- An offline step computes per-site, per-month-of-year sufficient stats from the
-- ERA5 reanalysis and uploads climatology.json to SeaweedFS out-of-band. The
-- stars.load_climatology job wholesale-replaces this table from that object, and
-- /api/stars/history sums it with the live accumulator per site. Same shape as
-- site_month_stats so the two compose by simple per-field addition.
CREATE TABLE stars.site_month_climatology (
    site_id      TEXT NOT NULL,
    month        SMALLINT NOT NULL,
    window_count INTEGER NOT NULL DEFAULT 0,
    sum_q        DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_darkness DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_clarity  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (site_id, month)
);
