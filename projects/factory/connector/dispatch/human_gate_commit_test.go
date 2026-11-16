package dispatch_test

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// gateCommitFault wraps the real PostgreSQL driver. Only the first gate send's
// COMMIT loses its acknowledgment, after the underlying transaction commits.
// Cleanup writes fail until the test restores the database path.
type gateCommitFault struct {
	armed        atomic.Bool
	committed    chan struct{}
	cleanupTried chan struct{}
	recovered    chan struct{}
	recoverOnce  sync.Once
}

func (f *gateCommitFault) recover() { f.recoverOnce.Do(func() { close(f.recovered) }) }

type gateFaultConnector struct {
	base  driver.Driver
	dsn   string
	fault *gateCommitFault
}

func (c *gateFaultConnector) Driver() driver.Driver { return c.base }

func (c *gateFaultConnector) Connect(_ context.Context) (driver.Conn, error) {
	conn, err := c.base.Open(c.dsn)
	if err != nil {
		return nil, err
	}
	return &gateFaultConn{Conn: conn, fault: c.fault}, nil
}

type gateFaultConn struct {
	driver.Conn
	fault      *gateCommitFault
	loseCommit bool
}

func (c *gateFaultConn) BeginTx(ctx context.Context, opts driver.TxOptions) (driver.Tx, error) {
	tx, err := c.Conn.(driver.ConnBeginTx).BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	c.loseCommit = false
	return &gateFaultTx{Tx: tx, conn: c}, nil
}

func (c *gateFaultConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	rows, err := c.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
	if err == nil && strings.Contains(query, "UPDATE human_gate_attempt SET send_started = true") && c.fault.armed.CompareAndSwap(true, false) {
		c.loseCommit = true
	}
	return rows, err
}

func (c *gateFaultConn) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	if strings.Contains(query, "UPDATE human_gate_attempt SET send_started = false") {
		select {
		case <-c.fault.recovered:
		default:
			select {
			case c.fault.cleanupTried <- struct{}{}:
			default:
			}
			return nil, errors.New("injected cleanup outage")
		}
	}
	return c.Conn.(driver.ExecerContext).ExecContext(ctx, query, args)
}

type gateFaultTx struct {
	driver.Tx
	conn *gateFaultConn
}

func (tx *gateFaultTx) Commit() error {
	if err := tx.Tx.Commit(); err != nil {
		return err
	}
	if tx.conn.loseCommit {
		tx.conn.loseCommit = false
		close(tx.conn.fault.committed)
		return driver.ErrBadConn // discard the real COMMIT acknowledgment
	}
	return nil
}

func TestHumanGateLostCommitAcknowledgmentSurvivesProcessLoss(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	db := v.st.DB
	var database string
	var port int
	if err := db.QueryRow(`SELECT current_database(), inet_server_port()`).Scan(&database, &port); err != nil {
		t.Fatal(err)
	}
	fault := &gateCommitFault{
		committed: make(chan struct{}), cleanupTried: make(chan struct{}, 8), recovered: make(chan struct{}),
	}
	fault.armed.Store(true)
	v.e.Release()
	v.st.DB = sql.OpenDB(&gateFaultConnector{
		base: db.Driver(), dsn: fmt.Sprintf("host=127.0.0.1 port=%d user=test dbname=%s sslmode=disable", port, database), fault: fault,
	})
	t.Cleanup(func() { db.Close() })
	v.e = v.engine("lost-gate-commit")
	v.clk.Advance(61 * time.Second)
	returned := make(chan error, 1)
	go func() { returned <- v.e.Tick(v.ctx) }()
	select {
	case err := <-returned:
		if !errors.Is(err, store.ErrNotWriter) {
			t.Fatalf("tick = %v, want writer loss", err)
		}
	case <-time.After(5 * time.Second):
		// Let a regressed synchronous retry drain before DB teardown.
		fault.recover()
		<-returned
		t.Fatal("dispatch blocked waiting for database recovery")
	}
	select {
	case <-fault.committed:
	default:
		t.Fatal("fault did not commit before dropping the acknowledgment")
	}
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	var started bool
	var sends int
	if err := db.QueryRow(`SELECT send_started, sends FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&started, &sends); err != nil || !started || sends != 1 {
		t.Fatalf("marker did not commit: started=%t sends=%d err=%v", started, sends, err)
	}
	if len(v.its.gateCreates) != 0 {
		t.Fatal("failed marker acknowledgment reached POST")
	}
	files, err := os.ReadDir(v.cfg.HumanGateJournal)
	if err != nil || len(files) != 1 {
		t.Fatalf("unsent proof missing from persistent storage: %v %v", files, err)
	}
	// Discard the entire engine while cleanup is still unavailable. There is
	// no shutdown recovery callback or in-memory proof passed to its successor.
	v.e.Release()
	v.e = v.engine("after-process-loss")
	v.poll()
	select {
	case <-fault.cleanupTried:
	default:
		t.Fatal("new process did not read and replay the persisted proof")
	}
	if len(v.its.gateCreates) != 0 {
		t.Fatal("failed cleanup repeated POST")
	}
	fault.recover()
	v.poll()
	v.gateIssue(1, "sha-1")
	v.poll()
	if len(v.its.gateCreates) != 1 {
		t.Fatal("replacement writer did not create exactly one issue")
	}
	files, err = os.ReadDir(v.cfg.HumanGateJournal)
	if err != nil || len(files) != 0 {
		t.Fatalf("acknowledged recovery left stale proof: %v %v", files, err)
	}
}

func TestHumanGateRefusedCreateCleanupCannotBlockDispatch(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.gateCreateErr = &itsaplan.HTTPError{Status: 403, Body: "refused"}
	v.poll()
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	tx, err := v.st.DB.BeginTx(v.ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`SELECT message_id FROM human_gate_attempt WHERE message_id = $1 FOR UPDATE`, messageID); err != nil {
		t.Fatal(err)
	}
	// An unrelated outbox item is after human-gate work in the singleton tick.
	if err := v.st.Enqueue(v.ctx, v.st.DB, "comment", "after-locked-gate", 0, "", map[string]any{
		"issueId": 101, "body": "dispatch continued after locked cleanup",
	}); err != nil {
		t.Fatal(err)
	}
	before := len(v.its.Comments())
	// Keep the PR below its poll interval: only recovery, not a new write,
	// encounters the lock. Repeated ticks must stay bounded while it is held.
	for range 2 {
		ctx, cancel := context.WithTimeout(v.ctx, 3*time.Second)
		started := time.Now()
		err := v.e.Tick(ctx)
		cancel()
		if err != nil || time.Since(started) >= 2500*time.Millisecond {
			t.Fatalf("locked recovery blocked dispatch: %v (%v)", err, time.Since(started))
		}
	}
	if len(v.its.Comments()) != before+1 {
		t.Fatal("locked gate cleanup blocked the later outbox phase")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	v.its.gateCreateErr = nil
	v.poll()
	v.gateIssue(1, "sha-1")
	if len(v.its.gateCreates) != 2 {
		t.Fatal("refused create did not retry exactly once after recovery")
	}
}

func TestHumanGateCleanupWaitsForInFlightMarker(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	v.its.gateCreateErr = &itsaplan.HTTPError{Status: 403, Body: "refused"}
	v.poll()
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	tx, err := v.st.DB.BeginTx(v.ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var send int
	if err := tx.QueryRow(`UPDATE human_gate_attempt SET send_started = true, sends = sends + 1
		WHERE message_id = $1 RETURNING sends`, messageID).Scan(&send); err != nil {
		t.Fatal(err)
	}
	cleared := make(chan error, 1)
	go func() { cleared <- v.st.ClearHumanGateSend(v.ctx, messageID, v.e.Epoch(), send) }()
	select {
	case err := <-cleared:
		t.Fatalf("cleanup returned before the marker transaction finished: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-cleared:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cleanup did not finish after the marker committed")
	}
	var started bool
	if err := v.st.DB.QueryRow(`SELECT send_started FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&started); err != nil || started {
		t.Fatalf("late marker escaped cleanup: started=%t err=%v", started, err)
	}
}
