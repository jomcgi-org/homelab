-- hikes walk_hours: keep every daylight hour and the complete forecast's
-- extra fields, for the Scotland connector's per-caller weather limits.
--
-- The forecast job now stores every daylight hour, good or not, and readers
-- that want the hikes app's good hours filter on precip_mm <= 2 and
-- wind_kmh <= 80 (hikes.forecast.is_viable). wind_kmh is now km/h to two
-- decimals, exact for met.no's one-decimal m/s, so that filter equals the
-- viability rule at the boundary. The new columns are nullable: met.no can
-- leave any of them out, and rows already stored predate them until the next
-- refresh replaces them.

ALTER TABLE hikes.walk_hours
    ADD COLUMN gust_kmh            DOUBLE PRECISION,
    ADD COLUMN precip_max_mm       DOUBLE PRECISION,
    ADD COLUMN rain_chance_pct     DOUBLE PRECISION,
    ADD COLUMN thunder_chance_pct  DOUBLE PRECISION,
    ADD COLUMN fog_pct             DOUBLE PRECISION,
    ADD COLUMN low_cloud_pct       DOUBLE PRECISION;
