package dispatch_test

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// gateCommitFault wraps the real PostgreSQL driver. Only the first gate send's
// COMMIT loses its acknowledgment, after the underlying transaction commits.
type gateCommitFault struct {
	armed     atomic.Bool
	committed chan struct{}
}

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

// A lost COMMIT acknowledgment leaves a started send that never reached
// itsaplan. The replacement process waits for the send to settle, finds no
// marked issue and creates exactly one.
func TestHumanGateLostCommitAcknowledgmentCreatesOnceAfterSettling(t *testing.T) {
	v := newHumanGateEnv(t)
	v.readyGate("sha-1")
	db := v.st.DB
	var database string
	var port int
	if err := db.QueryRow(`SELECT current_database(), inet_server_port()`).Scan(&database, &port); err != nil {
		t.Fatal(err)
	}
	fault := &gateCommitFault{committed: make(chan struct{})}
	fault.armed.Store(true)
	v.e.Release()
	v.st.DB = sql.OpenDB(&gateFaultConnector{
		base: db.Driver(), dsn: fmt.Sprintf("host=127.0.0.1 port=%d user=test dbname=%s sslmode=disable", port, database), fault: fault,
	})
	t.Cleanup(func() { db.Close() })
	v.e = v.engine("lost-gate-commit")
	v.clk.Advance(61 * time.Second)
	if err := v.e.Tick(v.ctx); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("tick = %v, want writer loss", err)
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
	v.e.Release()
	v.e = v.engine("after-process-loss")
	v.poll()
	if len(v.its.gateCreates) != 0 {
		t.Fatal("an unsettled send was repeated")
	}
	v.settleGateSends()
	v.poll()
	v.gateIssue(1, "sha-1")
	v.poll()
	if len(v.its.gateCreates) != 1 || v.gateIssues() != 1 {
		t.Fatal("replacement writer did not create exactly one issue")
	}
}
