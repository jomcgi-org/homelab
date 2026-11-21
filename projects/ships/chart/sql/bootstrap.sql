-- The publisher's login. It owns nothing and only reads the tables that the
-- grants at the end of this file name. CloudNativePG sets LOGIN and the
-- password from managed.roles (templates/database.yaml).
DO $$
BEGIN
    CREATE ROLE ships_publisher NOLOGIN NOINHERIT;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

SET ROLE ships;

CREATE SCHEMA IF NOT EXISTS ships;

-- Reserved for the ingester lease. The publisher receives no privileges.
CREATE TABLE ships.ingest_lease ();

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

CREATE TABLE ships.positions_default PARTITION OF ships.positions DEFAULT;

CREATE INDEX positions_recorded_brin ON ships.positions USING brin (recorded_at);
CREATE INDEX positions_mmsi_recorded ON ships.positions (mmsi, recorded_at DESC);

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

CREATE TABLE ships.heat_cells (
    lat_bin      INTEGER NOT NULL,
    lon_bin      INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat_bin, lon_bin)
);

CREATE TABLE ships.heat_cells_historical (
    lat_bin     INTEGER NOT NULL,
    lon_bin     INTEGER NOT NULL,
    count       BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lat_bin, lon_bin)
);

-- The writer commits received frames here before it applies them. A frame
-- leaves the inbox in the transaction that applies it.
CREATE TABLE ships.inbox (
    id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    frame  BYTEA NOT NULL
);

-- The publisher reads these five tables and nothing else. It cannot write,
-- lock for update, create, alter or drop. A query through ships.positions
-- checks only the parent's privileges, so new partitions need no grant.
GRANT USAGE ON SCHEMA ships TO ships_publisher;
GRANT SELECT ON ships.vessels, ships.positions, ships.latest_positions,
    ships.heat_cells, ships.heat_cells_historical TO ships_publisher;
