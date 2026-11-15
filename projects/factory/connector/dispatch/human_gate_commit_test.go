package dispatch_test

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
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

func TestHumanGateLostCommitAcknowledgmentRecoversBeforeExit(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	// Reconnect the writer through the faulting driver to the same real DB.
	// Keep the original pool for observations unaffected by the injected fault.
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
	ctx, cancel := context.WithCancel(v.ctx)
	defer cancel()
	v.clk.Advance(61 * time.Second)
	returned := make(chan error, 1)
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		returned <- v.e.Tick(ctx)
	}()
	// Always let recovery drain before releasing the test database, including
	// if an assertion fails while the dispatch goroutine is waiting to retry.
	defer func() {
		fault.recover()
		select {
		case <-finished:
		case <-time.After(10 * time.Second):
			t.Error("dispatch did not finish after database recovery")
		}
	}()
	wait := func(signal <-chan struct{}, what string) {
		t.Helper()
		select {
		case <-signal:
		case err := <-returned:
			t.Fatalf("dispatch exited before %s: %v", what, err)
		case <-time.After(10 * time.Second):
			t.Fatalf("timed out waiting for %s", what)
		}
	}
	wait(fault.committed, "the committed marker")
	const messageID = "factory-human-gate/jomcgi-org/homelab/1/create"
	var started bool
	var sends int
	if err := db.QueryRow(`SELECT send_started, sends FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&started, &sends); err != nil || !started || sends != 1 {
		t.Fatalf("marker did not commit: started=%t sends=%d err=%v", started, sends, err)
	}
	wait(fault.cleanupTried, "the first refused cleanup")
	cancel() // losing the tick context must not discard the unsent proof
	wait(fault.cleanupTried, "a cleanup retry after cancellation")
	select {
	case err := <-returned:
		t.Fatalf("dispatch discarded unsent proof while cleanup was unavailable: %v", err)
	default:
	}
	fault.recover()
	select {
	case err := <-returned:
		if !errors.Is(err, store.ErrNotWriter) {
			t.Fatalf("tick = %v, want the original writer error after recovery", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("dispatch did not return after persisting unsent proof")
	}
	if len(v.its.gateCreates) != 0 {
		t.Fatal("failed marker acknowledgment reached POST")
	}
	if err := db.QueryRow(`SELECT send_started, sends FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&started, &sends); err != nil || started || sends != 1 {
		t.Fatalf("unsent proof was not durable: started=%t sends=%d err=%v", started, sends, err)
	}
	v.e.Release()
	v.e = v.engine("after-lost-gate-commit")
	v.poll()
	v.gateIssue(1, "sha-1")
	v.poll()
	if len(v.its.gateCreates) != 1 {
		t.Fatal("replacement writer did not create exactly one issue")
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
