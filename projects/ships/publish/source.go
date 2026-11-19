package publish

import (
	"context"
	"database/sql"
	"time"

	"github.com/lib/pq"
)

// reader reads one consistent view of the Ships database.
type reader interface {
	// at returns the database time of the view.
	at() time.Time
	vessels(ctx context.Context) ([]vessel, error)
	// tracks calls each once for every MMSI with retained positions, with
	// up to trackLimit points, newest first.
	tracks(ctx context.Context, mmsis []string, each func(mmsi string, points []point) error) error
	heat(ctx context.Context) (heat, error)
	close() error
}

// pgReader reads in one read-only REPEATABLE READ transaction, so the
// snapshot, its tracks and the heat grid come from one database snapshot.
type pgReader struct {
	tx  *sql.Tx
	now time.Time
}

func openReader(ctx context.Context, db *sql.DB) (reader, error) {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return nil, err
	}
	r := &pgReader{tx: tx}
	if err := tx.QueryRowContext(ctx, "select transaction_timestamp()").Scan(&r.now); err != nil {
		tx.Rollback()
		return nil, err
	}
	r.now = r.now.UTC()
	return r, nil
}

func (r *pgReader) at() time.Time { return r.now }

func (r *pgReader) close() error { return r.tx.Rollback() }

// The hub's snapshot query, ordered so that equal data gives equal bytes.
const vesselsSQL = `select lp.mmsi, lp.lat, lp.lon, lp.speed, lp.course, lp.heading, lp.nav_status,
 coalesce(nullif(lp.ship_name, ''), v.name), lp.recorded_at, lp.first_seen_at_location, lp.updated_at,
 v.name, v.ship_type, v.destination, v.eta
 from ships.latest_positions lp left join ships.vessels v on v.mmsi = lp.mmsi
 order by lp.mmsi`

func (r *pgReader) vessels(ctx context.Context) ([]vessel, error) {
	rows, err := r.tx.QueryContext(ctx, vesselsSQL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []vessel
	for rows.Next() {
		var v vessel
		var first, eta *time.Time
		if err := rows.Scan(&v.MMSI, &v.Lat, &v.Lon, &v.Speed, &v.Course, &v.Heading, &v.NavStatus,
			&v.ShipName, &v.RecordedAt.Time, &first, &v.UpdatedAt.Time,
			&v.Name, &v.ShipType, &v.Destination, &eta); err != nil {
			return nil, err
		}
		v.FirstSeenAtLocation, v.ETA = stampOf(first), stampOf(eta)
		out = append(out, v)
	}
	return out, rows.Err()
}

// The hub's track query for a batch of vessels. The id breaks ties, so equal
// history gives equal bytes.
const tracksSQL = `select v.mmsi, p.lat, p.lon, p.speed, p.course, p.heading, p.nav_status, p.recorded_at
 from unnest($1::text[]) as v(mmsi)
 cross join lateral (
   select id, lat, lon, speed, course, heading, nav_status, recorded_at
   from ships.positions where positions.mmsi = v.mmsi
   order by recorded_at desc, id desc limit $2) p
 order by v.mmsi, p.recorded_at desc, p.id desc`

func (r *pgReader) tracks(ctx context.Context, mmsis []string, each func(string, []point) error) error {
	rows, err := r.tx.QueryContext(ctx, tracksSQL, pq.Array(mmsis), trackLimit)
	if err != nil {
		return err
	}
	defer rows.Close()
	current := ""
	var points []point
	for rows.Next() {
		var mmsi string
		var p point
		if err := rows.Scan(&mmsi, &p.Lat, &p.Lon, &p.Speed, &p.Course, &p.Heading, &p.NavStatus, &p.RecordedAt.Time); err != nil {
			return err
		}
		if mmsi != current && len(points) > 0 {
			if err := each(current, points); err != nil {
				return err
			}
			points = nil
		}
		current = mmsi
		points = append(points, p)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(points) > 0 {
		return each(current, points)
	}
	return nil
}

// The live seven-day rollup plus the banked all-time counts, as the hub sums them.
const heatSQL = `select lat_bin, lon_bin, sum(count)::bigint from (
   select lat_bin, lon_bin, count::bigint from ships.heat_cells
   union all
   select lat_bin, lon_bin, count from ships.heat_cells_historical) cells
 group by 1, 2 order by 1, 2`

func (r *pgReader) heat(ctx context.Context) (heat, error) {
	var computed *time.Time
	if err := r.tx.QueryRowContext(ctx, "select max(computed_at) from ships.heat_cells").Scan(&computed); err != nil {
		return heat{}, err
	}
	rows, err := r.tx.QueryContext(ctx, heatSQL)
	if err != nil {
		return heat{}, err
	}
	defer rows.Close()
	var cells []cell
	for rows.Next() {
		var c cell
		if err := rows.Scan(&c[0], &c[1], &c[2]); err != nil {
			return heat{}, err
		}
		cells = append(cells, c)
	}
	if err := rows.Err(); err != nil {
		return heat{}, err
	}
	if computed != nil {
		t := computed.UTC()
		computed = &t
	}
	return newHeat(computed, cells), nil
}
