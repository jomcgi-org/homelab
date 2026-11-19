SET LOCAL TIME ZONE INTERVAL '0 hours';
DO $$
DECLARE
  d date;
  part text;
BEGIN
  FOR d IN SELECT current_date + offset_days FROM generate_series(-7, 2) AS offset_days LOOP
    EXECUTE format('CREATE TABLE IF NOT EXISTS ships.%I PARTITION OF ships.positions FOR VALUES FROM (%L) TO (%L)',
                   'positions_' || to_char(d,'YYYYMMDD'), d, d+1);
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
