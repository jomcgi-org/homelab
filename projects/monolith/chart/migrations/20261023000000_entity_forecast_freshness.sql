-- Successful empty forecasts have no hour rows. Keep their fetch time on the
-- entity independently of pruning. Legacy rows remain unknown until fetched.
ALTER TABLE hikes.walks ADD COLUMN forecast_fetched_at TIMESTAMPTZ;
ALTER TABLE stars.sites ADD COLUMN forecast_fetched_at TIMESTAMPTZ;
