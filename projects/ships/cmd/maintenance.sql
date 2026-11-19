SET LOCAL TIME ZONE INTERVAL '0 hours';
DO $$
DECLARE
  d date;
  part text;
BEGIN
  FOR d IN SELECT current_date + offset_days FROM generate_series(-7, 2) AS offset_days LOOP
    part := 'positions_' || to_char(d,'YYYYMMDD');
    CONTINUE WHEN to_regclass(format('ships.%I', part)) IS NOT NULL;
    -- Rows that reached the default partition move into the new day first,
    -- because PostgreSQL refuses a partition whose range the default holds.
    EXECUTE format('CREATE TABLE ships.%I (LIKE ships.positions)', part);
    EXECUTE format('WITH moved AS (DELETE FROM ships.positions_default
                      WHERE recorded_at >= %L AND recorded_at < %L RETURNING *)
                    INSERT INTO ships.%I SELECT * FROM moved', d, d+1, part);
    EXECUTE format('ALTER TABLE ships.positions ATTACH PARTITION ships.%I FOR VALUES FROM (%L) TO (%L)',
                   part, d, d+1);
  END LOOP;

  IF current_setting('ships.retention') = 'on' THEN
    FOR part IN SELECT relname FROM pg_partition_tree('ships.positions') tree
                JOIN pg_class c ON tree.relid = c.oid
                WHERE tree.isleaf AND relname ~ '^positions_[0-9]{8}$' LOOP
      d := to_date(right(part,8),'YYYYMMDD');
      IF d >= current_date - 7 THEN CONTINUE; END IF;
      EXECUTE format('INSERT INTO ships.heat_cells_historical(lat_bin,lon_bin,count)
        SELECT floor(lat/0.005),floor(lon/0.0075),count(DISTINCT mmsi)
        FROM ships.%1$I JOIN (SELECT mmsi FROM ships.%1$I GROUP BY mmsi HAVING max(speed)>=1) movers USING(mmsi)
        WHERE lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 AND (lat<>0 OR lon<>0)
        GROUP BY 1,2 ON CONFLICT(lat_bin,lon_bin) DO UPDATE
        SET count=ships.heat_cells_historical.count+excluded.count, updated_at=now()',part);
      EXECUTE format('DROP TABLE ships.%I',part);
    END LOOP;
  END IF;
END $$;

DELETE FROM ships.heat_cells;
INSERT INTO ships.heat_cells(lat_bin,lon_bin,count)
WITH retained AS (SELECT * FROM ships.positions WHERE recorded_at >= now()-interval '7 days'),
movers AS (SELECT mmsi FROM retained GROUP BY mmsi HAVING max(speed)>=1)
SELECT floor(lat/0.005),floor(lon/0.0075),count(DISTINCT mmsi)
FROM retained JOIN movers USING(mmsi)
WHERE lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 AND (lat<>0 OR lon<>0)
GROUP BY 1,2;
