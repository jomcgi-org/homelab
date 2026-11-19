package main

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"log"
	"math"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/lib/pq"
	"nhooyr.io/websocket"
)

const (
	batchSize     = 200 // frames in one inbox write and one apply transaction
	flushInterval = 2 * time.Second
	readLimit     = 64 << 10
	readTimeout   = 30 * time.Second
	// A report dated further ahead of the writer clock would hold the latest
	// position until real time reaches it, so the writer drops it.
	maxClockSkew = 5 * time.Minute
	// After SIGTERM the writer has this long to commit the frames it received.
	stopGrace = 10 * time.Second
)

var (
	malformed atomic.Uint64 // frames without JSON or an MMSI
	rejected  atomic.Uint64 // reports dated too far ahead or refused by PostgreSQL
)

type position struct {
	Latitude, Longitude, Sog, Cog   *float64
	TrueHeading, NavigationalStatus *int
}
type vessel struct {
	Name, CallSign, Destination string
	ImoNumber                   json.Number
	Type                        *int
	Dimension                   struct{ A, B, C, D *int }
	MaximumStaticDraught        *float64
	Eta                         struct{ Month, Day, Hour, Minute *int }
}
type frame struct {
	MessageType string
	MetaData    struct {
		MMSI     json.Number
		ShipName string
		Time     string `json:"time_utc"`
	}
	Message struct {
		PositionReport *position
		ShipStaticData *vessel
	}
}

func (v vessel) eta(now time.Time) any {
	e := v.Eta
	if e.Month == nil || e.Day == nil {
		return nil
	}
	hour, minute := 0, 0
	if e.Hour != nil && *e.Hour != 24 {
		hour = *e.Hour
	}
	if e.Minute != nil && *e.Minute != 60 {
		minute = *e.Minute
	}
	year := now.Year()
	for i := 0; i < 2; i++ {
		t := time.Date(year+i, time.Month(*e.Month), *e.Day, hour, minute, 0, 0, time.UTC)
		if int(t.Month()) != *e.Month || t.Day() != *e.Day || t.Hour() != hour || t.Minute() != minute {
			return nil
		}
		if !t.Before(now) {
			return t
		}
	}
	return nil
}

func store(ctx context.Context, tx *sql.Tx, f frame) error {
	id := f.MetaData.MMSI.String()
	if id == "" {
		return nil
	}
	name := strings.TrimSpace(f.MetaData.ShipName)
	if v := f.Message.ShipStaticData; f.MessageType == "ShipStaticData" && v != nil {
		if n := strings.TrimSpace(v.Name); n != "" {
			name = n
		}
		_, err := tx.ExecContext(ctx, `insert into ships.vessels as old
 (mmsi,imo,call_sign,name,ship_type,dimension_a,dimension_b,dimension_c,dimension_d,destination,eta,draught)
 values ($1,nullif($2,''),nullif($3,''),nullif($4,''),$5,$6,$7,$8,$9,nullif($10,''),$11,$12)
 on conflict(mmsi) do update set imo=coalesce(excluded.imo,old.imo),call_sign=coalesce(excluded.call_sign,old.call_sign),
 name=coalesce(excluded.name,old.name),ship_type=coalesce(excluded.ship_type,old.ship_type),
 dimension_a=coalesce(excluded.dimension_a,old.dimension_a),dimension_b=coalesce(excluded.dimension_b,old.dimension_b),
 dimension_c=coalesce(excluded.dimension_c,old.dimension_c),dimension_d=coalesce(excluded.dimension_d,old.dimension_d),
 destination=coalesce(excluded.destination,old.destination),eta=coalesce(excluded.eta,old.eta),draught=coalesce(excluded.draught,old.draught),updated_at=now()`,
			id, v.ImoNumber.String(), strings.TrimSpace(v.CallSign), name, v.Type, v.Dimension.A, v.Dimension.B, v.Dimension.C, v.Dimension.D, strings.TrimSpace(v.Destination), v.eta(time.Now().UTC()), v.MaximumStaticDraught)
		return err
	}
	p := f.Message.PositionReport
	if f.MessageType != "PositionReport" || p == nil || p.Latitude == nil || p.Longitude == nil {
		return nil
	}
	at, err := time.Parse(time.RFC3339Nano, f.MetaData.Time)
	if err != nil {
		at, err = time.Parse("2006-01-02 15:04:05 -0700 MST", f.MetaData.Time)
	}
	if err != nil {
		return nil
	}
	if at.After(time.Now().Add(maxClockSkew)) {
		rejected.Add(1)
		return nil
	}
	var lat, lon float64
	var prior time.Time
	var first sql.NullTime
	err = tx.QueryRowContext(ctx, "select lat,lon,recorded_at,first_seen_at_location from ships.latest_positions where mmsi=$1", id).Scan(&lat, &lon, &prior, &first)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	firstSeen := at
	if err == nil {
		rad := math.Pi / 180
		a := math.Pow(math.Sin((*p.Latitude-lat)*rad/2), 2) + math.Cos(lat*rad)*math.Cos(*p.Latitude*rad)*math.Pow(math.Sin((*p.Longitude-lon)*rad/2), 2)
		meters := 12742000 * math.Asin(math.Sqrt(math.Min(a, 1)))
		if meters <= 100 && (p.Sog == nil || *p.Sog <= 0.5) && at.Sub(prior) <= 5*time.Minute {
			return nil
		}
		if meters <= 500 && first.Valid {
			firstSeen = first.Time
		}
	}
	// An older report joins the history. The WHERE clause keeps the newer latest position.
	_, err = tx.ExecContext(ctx, `with added as (
 insert into ships.positions(mmsi,lat,lon,speed,course,heading,nav_status,ship_name,recorded_at)
 values($1,$2,$3,$4,$5,$6,$7,nullif($8,''),$9) returning *)
 insert into ships.latest_positions as old (mmsi,lat,lon,speed,course,heading,nav_status,ship_name,recorded_at,first_seen_at_location)
 select mmsi,lat,lon,speed,course,heading,nav_status,ship_name,recorded_at,$10 from added
 on conflict(mmsi) do update set lat=excluded.lat,lon=excluded.lon,speed=excluded.speed,course=excluded.course,
 heading=excluded.heading,nav_status=excluded.nav_status,ship_name=coalesce(excluded.ship_name,old.ship_name),
 recorded_at=excluded.recorded_at,first_seen_at_location=excluded.first_seen_at_location,updated_at=now()
 where old.recorded_at <= excluded.recorded_at`,
		id, p.Latitude, p.Longitude, p.Sog, p.Cog, p.TrueHeading, p.NavigationalStatus, name, at, firstSeen)
	return err
}

// dataError reports a PostgreSQL data exception or constraint violation: a
// frame that fails every retry, so the writer drops it.
func dataError(err error) bool {
	var pqErr *pq.Error
	return errors.As(err, &pqErr) && (pqErr.Code.Class() == "22" || pqErr.Code.Class() == "23")
}

// spool commits received frames to ships.inbox before the writer applies them.
func spool(ctx context.Context, c *sql.Conn, frames [][]byte) error {
	_, err := c.ExecContext(ctx, `insert into ships.inbox(frame)
 select frame from unnest($1::bytea[]) with ordinality as received(frame, n) order by n`, pq.ByteaArray(frames))
	return err
}

// apply moves spooled frames into history, latest positions and vessels in
// receive order. A frame leaves ships.inbox in the transaction that applies it.
func apply(ctx context.Context, c *sql.Conn) error {
	for {
		n, err := applyChunk(ctx, c, false)
		if dataError(err) {
			n, err = applyChunk(ctx, c, true)
		}
		if err != nil || n < batchSize {
			return err
		}
	}
}

// applyChunk applies the oldest spooled frames. With isolate set, a savepoint
// around each frame lets the chunk commit without a frame PostgreSQL refuses.
func applyChunk(ctx context.Context, c *sql.Conn, isolate bool) (int, error) {
	tx, err := c.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, "select id, frame from ships.inbox order by id limit $1", batchSize)
	if err != nil {
		return 0, err
	}
	var last int64
	var frames [][]byte
	for rows.Next() {
		var raw []byte
		if err = rows.Scan(&last, &raw); err != nil {
			rows.Close()
			return 0, err
		}
		frames = append(frames, raw)
	}
	if err = rows.Close(); err != nil {
		return 0, err
	}
	if err = rows.Err(); err != nil || len(frames) == 0 {
		return 0, err
	}
	for _, raw := range frames {
		var f frame
		if json.Unmarshal(raw, &f) != nil {
			continue // The reader spools only frames that decode.
		}
		if isolate {
			if _, err = tx.ExecContext(ctx, "savepoint frame"); err != nil {
				return 0, err
			}
		}
		err = store(ctx, tx, f)
		if isolate && dataError(err) {
			rejected.Add(1)
			_, err = tx.ExecContext(ctx, "rollback to savepoint frame")
		}
		if err == nil && isolate {
			_, err = tx.ExecContext(ctx, "release savepoint frame")
		}
		if err != nil {
			return 0, err
		}
	}
	if _, err = tx.ExecContext(ctx, "delete from ships.inbox where id <= $1", last); err != nil {
		return 0, err
	}
	return len(frames), tx.Commit()
}

//go:embed maintenance.sql
var maintenanceSQL string

func maintain(ctx context.Context, c *sql.Conn) error {
	tx, err := c.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	retention := "off"
	if os.Getenv("SHIPS_RETENTION_ENABLED") == "true" {
		retention = "on"
	}
	if _, err = tx.ExecContext(ctx, "select set_config('ships.retention',$1,true)", retention); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, maintenanceSQL); err != nil {
		return err
	}
	return tx.Commit()
}

// read sends each frame that decodes with an MMSI to the queue. It returns
// when the feed fails or ctx ends, and closes the queue.
func read(ctx context.Context, ws *websocket.Conn, queue chan<- []byte) {
	defer close(queue)
	for {
		readCtx, cancel := context.WithTimeout(ctx, readTimeout)
		_, raw, err := ws.Read(readCtx)
		cancel()
		if err != nil {
			return
		}
		var f frame
		if json.Unmarshal(raw, &f) != nil || f.MetaData.MMSI == "" {
			malformed.Add(1)
			continue
		}
		select {
		case queue <- raw:
		case <-ctx.Done():
			return
		}
	}
}

func ingest(ctx context.Context, db *sql.DB, key string) error {
	// Database work continues for stopGrace after SIGTERM, so received frames commit.
	work, cancelWork := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelWork()
	defer context.AfterFunc(ctx, func() { time.AfterFunc(stopGrace, cancelWork) })()
	c, err := db.Conn(work)
	if err != nil {
		return err
	}
	defer c.Close()
	var held bool
	if err = c.QueryRowContext(work, "select pg_try_advisory_lock(409,1)").Scan(&held); err != nil {
		return err
	}
	if !held {
		return errors.New("database lease unavailable")
	}
	if err = maintain(work, c); err != nil {
		return err
	}
	nextMaintenance := time.Now().Add(time.Hour)
	// Recover the frames that an interrupted session spooled but did not apply.
	if err = apply(work, c); err != nil {
		return err
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	url := os.Getenv("AISSTREAM_URL")
	if url == "" {
		url = "wss://stream.aisstream.io/v0/stream"
	}
	ws, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		return err
	}
	defer ws.CloseNow()
	sub, err := json.Marshal(map[string]any{"APIKey": key, "BoundingBoxes": [][][]float64{{{46.876152, -129.552155}, {51.413769, -121.213531}}}, "FilterMessageTypes": []string{"PositionReport", "ShipStaticData"}})
	if err != nil {
		return err
	}
	if err = ws.Write(ctx, websocket.MessageText, sub); err != nil {
		return err
	}
	ws.SetReadLimit(readLimit)
	queue := make(chan []byte, batchSize)
	go read(ctx, ws, queue)
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	batch := make([][]byte, 0, batchSize)
	var committed time.Time
	defer func() {
		log.Printf("session ended: malformed=%d rejected=%d last_commit=%v", malformed.Load(), rejected.Load(), committed)
	}()
	for {
		closed := false
		select {
		case raw, open := <-queue:
			if open {
				batch = append(batch, raw)
				if len(batch) < batchSize {
					continue
				}
			}
			closed = !open
		case <-ticker.C:
		}
		if len(batch) == 0 {
			err = c.PingContext(work)
		} else if err = spool(work, c, batch); err != nil {
			log.Printf("inbox write failed: lost_frames=%d", len(batch))
		} else {
			clear(batch)
			batch = batch[:0]
			if err = apply(work, c); err == nil {
				committed = time.Now().UTC()
			}
		}
		if err != nil {
			return err
		}
		if closed {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return errors.New("feed closed")
		}
		if time.Now().After(nextMaintenance) {
			if err = maintain(work, c); err != nil {
				return err
			}
			nextMaintenance = time.Now().Add(time.Hour)
		}
	}
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	key, dsn := os.Getenv("AISSTREAM_API_KEY"), os.Getenv("DATABASE_URL")
	if key == "" || dsn == "" {
		log.Fatal("AISSTREAM_API_KEY and DATABASE_URL required")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		log.Fatal("invalid database configuration")
	}
	defer db.Close()
	db.SetMaxIdleConns(0) // Release the session lock whenever the socket closes.
	for delay := time.Second; ctx.Err() == nil; {
		started := time.Now()
		if err := ingest(ctx, db, key); err != nil && ctx.Err() == nil {
			log.Printf("ingest session ended: %v", err)
		}
		if time.Since(started) > time.Minute {
			delay = time.Second
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, time.Minute)
	}
}
