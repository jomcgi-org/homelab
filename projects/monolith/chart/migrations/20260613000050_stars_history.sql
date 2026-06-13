-- Stars historical heatmap (ADR 008): bank realized quality as forecast hours
-- elapse, bucketed by calendar month. The hourly prune accumulates the
-- component sums into stars.site_month_stats before deleting the elapsed rows,
-- so the history survives the prune that otherwise throws it away.
--
-- site_hours gains darkness_factor and cloud_factor (refresh already computes
-- them) so the prune can bank the decomposed signal (darkness, clarity) in one
-- grouped accumulate, not just the lossy Q sum.

ALTER TABLE stars.site_hours ADD COLUMN darkness_factor DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE stars.site_hours ADD COLUMN cloud_factor DOUBLE PRECISION NOT NULL DEFAULT 0;

-- Month-of-year (1-12), not year-month, so each calendar month accumulates
-- across all years into a stable seasonal climatology and the table stays
-- bounded at 12 rows per site.
CREATE TABLE stars.site_month_stats (
    site_id      TEXT NOT NULL,
    month        SMALLINT NOT NULL,
    window_count INTEGER NOT NULL DEFAULT 0,
    sum_q        DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_darkness DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_clarity  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (site_id, month)
);
