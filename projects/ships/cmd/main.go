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
	// A report dated more than maxClockSkew after its frame arrives would hold
	// the latest position until real time reaches it, so the reader drops it
	// before the inbox write. A delayed apply cannot change that result.
	maxClockSkew = 5 * time.Minute
	// After SIGTERM the writer has this long to commit the frames it received.
	stopGrace = 10 * time.Second
	// The writer pings its lease connection every leaseCheck. A ping that
	// succeeds confirms the lease for leaseTimeout from the moment the writer
	// sent it. When the confirmation expires or a ping fails, the writer closes
	// its feed socket.
	leaseCheck   = time.Second
	leaseTimeout = 5 * time.Second
	// After a writer takes the lease and applies the inbox, it waits leaseGrace
	// before it subscribes. A running writer that lost the lease closes its
	// socket less than leaseTimeout after the loss, so one socket is open.
	leaseGrace = 2 * leaseTimeout
)

var (
	malformed atomic.Uint64 // frames without JSON or an MMSI
	ahead     atomic.Uint64 // reports dated more than maxClockSkew after arrival
	refused   atomic.Uint64 // reports PostgreSQL refuses
)

// errLeaseLost ends a session that cannot confirm its lease.
var errLeaseLost = errors.New("lease not confirmed")

func logCounters(event string, last time.Time) {
	log.Printf("%s: malformed=%d ahead=%d refused=%d last_apply=%v", event, malformed.Load(), ahead.Load(), refused.Load(), last)
}

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

// reportTime reads time_utc. A timestamp without an offset is UTC, as the hub
// parser reads it.
func reportTime(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05 -0700 MST", "2006-01-02T15:04:05", "2006-01-02 15:04:05"} {
		if at, err := time.Parse(layout, value); err == nil {
			return at, true
		}
	}
	return time.Time{}, false
}

// report returns a position report that has coordinates and a readable time.
// The writer ignores other position frames.
func (f frame) report() (*position, time.Time, bool) {
	p := f.Message.PositionReport
	if f.MessageType != "PositionReport" || p == nil || p.Latitude == nil || p.Longitude == nil {
		return nil, time.Time{}, false
	}
	at, ok := reportTime(f.MetaData.Time)
	return p, at, ok
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
	p, at, ok := f.report()
	if !ok {
		return nil
	}
	var lat, lon float64
	var prior time.Time
	var first sql.NullTime
	err := tx.QueryRowContext(ctx, "select lat,lon,recorded_at,first_seen_at_location from ships.latest_positions where mmsi=$1", id).Scan(&lat, &lon, &prior, &first)
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

// spool commits received frames to ships.inbox in receive order.
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
//
// FOR UPDATE claims the frames until the transaction ends. If PostgreSQL ends
// only the lease session, the old apply can still commit while a standby holds
// the lease. The standby then waits on the claimed rows and skips the rows the
// old apply deleted, so each frame reaches history once.
func applyChunk(ctx context.Context, c *sql.Conn, isolate bool) (int, error) {
	tx, err := c.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, "select id, frame from ships.inbox order by id limit $1 for update", batchSize)
	if err != nil {
		return 0, err
	}
	var ids []int64
	var frames [][]byte
	for rows.Next() {
		var id int64
		var raw []byte
		if err = rows.Scan(&id, &raw); err != nil {
			rows.Close()
			return 0, err
		}
		ids = append(ids, id)
		frames = append(frames, raw)
	}
	if err = rows.Close(); err != nil {
		return 0, err
	}
	if err = rows.Err(); err != nil || len(frames) == 0 {
		return 0, err
	}
	// The counter changes only when the chunk commits, so a retried chunk
	// counts each refused report once.
	var refusedHere uint64
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
			refusedHere++
			_, err = tx.ExecContext(ctx, "rollback to savepoint frame")
		}
		if err == nil && isolate {
			_, err = tx.ExecContext(ctx, "release savepoint frame")
		}
		if err != nil {
			return 0, err
		}
	}
	// The spooler commits new frames meanwhile, so delete only the claimed frames.
	deleted, err := tx.ExecContext(ctx, "delete from ships.inbox where id = any($1)", pq.Int64Array(ids))
	if err != nil {
		return 0, err
	}
	if n, err := deleted.RowsAffected(); err != nil || n != int64(len(ids)) {
		return 0, errors.Join(errors.New("inbox claim lost"), err)
	}
	if err = tx.Commit(); err != nil {
		return 0, err
	}
	refused.Add(refusedHere)
	return len(frames), nil
}

// lease is the advisory lock (409, 1). Its connection runs only the lock and
// the pings that confirm it, so a blocked write cannot delay a ping.
type lease struct {
	conn  *sql.Conn
	until atomic.Pointer[time.Time] // The lease is confirmed until this time.
}

func acquire(ctx context.Context, db *sql.DB) (*lease, error) {
	conn, err := db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	sent := time.Now()
	var held bool
	if err = conn.QueryRowContext(ctx, "select pg_try_advisory_lock(409,1)").Scan(&held); err == nil && !held {
		err = errors.New("database lease unavailable")
	}
	if err != nil {
		conn.Close()
		return nil, err
	}
	l := &lease{conn: conn}
	l.confirm(sent)
	return l, nil
}

// confirm records a query that succeeded on the lease connection. The
// confirmation counts from the moment the writer sent the query, before
// PostgreSQL ran it, so it ends less than leaseTimeout after a loss.
func (l *lease) confirm(sent time.Time) {
	until := sent.Add(leaseTimeout)
	l.until.Store(&until)
}

func (l *lease) held() bool { return time.Now().Before(*l.until.Load()) }

// keep pings the lease connection every leaseCheck until ctx ends. It calls
// lost when a ping fails or when the confirmation expires. A timer calls lost
// at expiry, so a ping that hangs on a dead connection cannot delay it.
func (l *lease) keep(ctx context.Context, lost func()) {
	expiry := time.AfterFunc(time.Until(*l.until.Load()), lost)
	defer expiry.Stop()
	tick := time.NewTicker(leaseCheck)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		sent := time.Now()
		if l.conn.PingContext(ctx) != nil {
			lost()
			return
		}
		l.confirm(sent)
		expiry.Reset(time.Until(*l.until.Load()))
	}
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

// read sends each frame that decodes with an MMSI to the queue, except a
// report dated more than maxClockSkew after the frame arrived. It returns when
// the feed fails, ctx ends or held reports false, and closes the queue.
func read(ctx context.Context, ws *websocket.Conn, queue chan<- []byte, held func() bool) {
	defer close(queue)
	for {
		readCtx, cancel := context.WithTimeout(ctx, readTimeout)
		_, raw, err := ws.Read(readCtx)
		cancel()
		// A frame read after the lease confirmation expires can also reach the
		// next lease holder, so the reader drops it.
		if err != nil || !held() {
			return
		}
		arrived := time.Now()
		var f frame
		if json.Unmarshal(raw, &f) != nil || f.MetaData.MMSI == "" {
			malformed.Add(1)
			continue
		}
		// Judge the date at arrival, before the frame reaches the inbox.
		if _, at, ok := f.report(); ok && at.After(arrived.Add(maxClockSkew)) {
			ahead.Add(1)
			continue
		}
		select {
		case queue <- raw:
		case <-ctx.Done():
			return
		}
	}
}

// spoolLoop commits queued frames to ships.inbox every flushInterval or
// batchSize frames and signals spooled after each commit. When the queue
// closes, it commits the frames it holds and returns.
func spoolLoop(ctx context.Context, c *sql.Conn, queue <-chan []byte, spooled chan<- struct{}) error {
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()
	batch := make([][]byte, 0, batchSize)
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
		if len(batch) > 0 {
			if err := spool(ctx, c, batch); err != nil {
				log.Printf("inbox write failed: lost_frames=%d", len(batch))
				return err
			}
			clear(batch)
			batch = batch[:0]
			select {
			case spooled <- struct{}{}:
			default: // The applier has an unread signal.
			}
		}
		if closed {
			return nil
		}
	}
}

// applyLoop applies ships.inbox after each spool signal and runs maintenance
// every hour. When spooled closes, it applies once more and returns.
func applyLoop(ctx context.Context, c *sql.Conn, spooled <-chan struct{}, last *time.Time) error {
	maintenance := time.NewTicker(time.Hour)
	defer maintenance.Stop()
	for {
		select {
		case _, open := <-spooled:
			if err := apply(ctx, c); err != nil {
				return err
			}
			*last = time.Now().UTC()
			if !open {
				return nil
			}
		case <-maintenance.C:
			if err := maintain(ctx, c); err != nil {
				return err
			}
			logCounters("ingest running", *last)
		}
	}
}

func ingest(ctx context.Context, db *sql.DB, key string) error {
	// Database work continues for stopGrace after SIGTERM, so received frames commit.
	work, cancelWork := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelWork()
	defer context.AfterFunc(ctx, func() { time.AfterFunc(stopGrace, cancelWork) })()
	l, err := acquire(work, db)
	if err != nil {
		return err
	}
	defer l.conn.Close()
	// The session ends when the lease is lost, and its end closes the socket.
	session, endSession := context.WithCancelCause(ctx)
	defer endSession(nil)
	go l.keep(session, func() { endSession(errLeaseLost) })
	// The inbox connection commits received frames. The history connection
	// applies them, so a blocked history write does not stop inbox commits.
	inbox, err := db.Conn(work)
	if err != nil {
		return err
	}
	defer inbox.Close()
	history, err := db.Conn(work)
	if err != nil {
		return err
	}
	defer history.Close()
	if err = maintain(work, history); err != nil {
		return err
	}
	// Recover the frames that an interrupted session spooled but did not apply.
	if err = apply(work, history); err != nil {
		return err
	}
	// A writer that lost the lease can still have its socket open for
	// leaseTimeout, so this writer waits longer before it subscribes.
	select {
	case <-session.Done():
		return context.Cause(session)
	case <-time.After(leaseGrace):
	}
	url := os.Getenv("AISSTREAM_URL")
	if url == "" {
		url = "wss://stream.aisstream.io/v0/stream"
	}
	ws, _, err := websocket.Dial(session, url, nil)
	if err != nil {
		return err
	}
	defer ws.CloseNow()
	// The socket closes as soon as the session ends, before the history
	// connection finishes its apply.
	defer context.AfterFunc(session, func() { ws.CloseNow() })()
	sub, err := json.Marshal(map[string]any{"APIKey": key, "BoundingBoxes": [][][]float64{{{46.876152, -129.552155}, {51.413769, -121.213531}}}, "FilterMessageTypes": []string{"PositionReport", "ShipStaticData"}})
	if err != nil {
		return err
	}
	if err = ws.Write(session, websocket.MessageText, sub); err != nil {
		return err
	}
	ws.SetReadLimit(readLimit)
	queue := make(chan []byte, batchSize)
	go read(session, ws, queue, l.held)
	spooled := make(chan struct{}, 1)
	applied := make(chan error, 1)
	var committed time.Time
	go func() {
		applyErr := applyLoop(work, history, spooled, &committed)
		if applyErr != nil {
			endSession(nil) // The reader stops and the spooler commits what it holds.
		}
		applied <- applyErr
	}()
	err = spoolLoop(work, inbox, queue, spooled)
	endSession(nil) // The socket closes now, while the applier finishes.
	if err != nil {
		cancelWork() // A blocked apply returns, and the inbox keeps its frames.
	}
	close(spooled)
	if applyErr := <-applied; err == nil {
		err = applyErr
	}
	logCounters("session ended", committed)
	if err != nil {
		return err
	}
	if cause := context.Cause(session); errors.Is(cause, errLeaseLost) {
		return cause
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return errors.New("feed closed")
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
