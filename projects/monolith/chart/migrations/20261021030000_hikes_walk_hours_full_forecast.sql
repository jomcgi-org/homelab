-- hikes walk_hours: keep every daylight hour, with the complete forecast's
-- fog and low cloud, for the Scotland connector's per-caller weather limits.
--
-- The forecast job now stores every daylight hour, good or not, and readers
-- that want the hikes app's good hours filter on precip_mm <= 2 and
-- wind_kmh <= 80 (hikes.forecast.is_viable). wind_kmh is now km/h to two
-- decimals, exact for met.no's one-decimal m/s, so that filter equals the
-- viability rule at the boundary. The new columns are nullable: met.no can
-- leave either out, and rows already stored predate them until the next
-- refresh replaces them. The complete forecast's gusts and rain chances are
-- not stored: met.no gives them only within its Nordic model, which in
-- Scotland covers Shetland only.

ALTER TABLE hikes.walk_hours
    ADD COLUMN fog_pct        DOUBLE PRECISION,
    ADD COLUMN low_cloud_pct  DOUBLE PRECISION;
