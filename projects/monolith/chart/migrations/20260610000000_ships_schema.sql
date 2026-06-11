-- ships schema: AIS vessel tracking.
--
-- vessels          : static/voyage metadata, one row per MMSI
-- positions        : partitioned position history (retention via drop-partition)
-- latest_positions : serving + dedup read-back table, one row per MMSI
--
-- Postgres is the single source of truth (stateless serving, no in-memory set).

CREATE SCHEMA IF NOT EXISTS ships;

-- One row per vessel (keyed by MMSI). Holds the static + voyage-related fields
-- that arrive on AIS type 5 messages and rarely change.
CREATE TABLE ships.vessels (
    mmsi         TEXT PRIMARY KEY,
    imo          TEXT,
    call_sign    TEXT,
    name         TEXT,
    ship_type    INTEGER,
    dimension_a  INTEGER,
    dimension_b  INTEGER,
    dimension_c  INTEGER,
    dimension_d  INTEGER,
    destination  TEXT,
    eta          TIMESTAMPTZ,
    draught      DOUBLE PRECISION,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only position history, range-partitioned on recorded_at so old data
-- can be dropped a partition at a time. A partitioned table's primary key must
-- include the partition key, hence the composite (recorded_at, id).
CREATE TABLE ships.positions (
    id           BIGINT NOT NULL,
    mmsi         TEXT NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed        DOUBLE PRECISION,
    course       DOUBLE PRECISION,
    heading      INTEGER,
    nav_status   INTEGER,
    ship_name    TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (recorded_at, id)
) PARTITION BY RANGE (recorded_at);

CREATE SEQUENCE ships.positions_id_seq OWNED BY ships.positions.id;
ALTER TABLE ships.positions ALTER COLUMN id SET DEFAULT nextval('ships.positions_id_seq');

-- Catch-all partition so inserts never fail before the maintenance job creates
-- per-period partitions.
CREATE TABLE ships.positions_default PARTITION OF ships.positions DEFAULT;

CREATE INDEX positions_recorded_brin ON ships.positions USING brin (recorded_at);
CREATE INDEX positions_mmsi_recorded ON ships.positions (mmsi, recorded_at DESC);

-- Serving + dedup read-back table: one row per MMSI holding the most recent
-- position. Read on ingest to dedup, and read by the API to serve the map.
CREATE TABLE ships.latest_positions (
    mmsi                    TEXT PRIMARY KEY,
    lat                     DOUBLE PRECISION NOT NULL,
    lon                     DOUBLE PRECISION NOT NULL,
    speed                   DOUBLE PRECISION,
    course                  DOUBLE PRECISION,
    heading                 INTEGER,
    nav_status              INTEGER,
    ship_name               TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL,
    first_seen_at_location  TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
