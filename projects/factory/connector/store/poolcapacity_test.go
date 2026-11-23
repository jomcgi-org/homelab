package store_test

import (
	"context"
	"testing"
	"time"
)

func TestWorkerPoolCapacityReadsTheReapersRow(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if _, found, err := st.WorkerPoolCapacity(ctx, "ns", "pool"); err != nil || found {
		t.Fatalf("an unwritten pool: found=%v err=%v", found, err)
	}
	if _, err := st.DB.Exec(`INSERT INTO worker_pool_capacity (namespace, pool, ready, busy, observed_at)
		VALUES ('ns', 'pool', 3, 1, now() - interval '20 seconds')`); err != nil {
		t.Fatal(err)
	}
	c, found, err := st.WorkerPoolCapacity(ctx, "ns", "pool")
	if err != nil || !found {
		t.Fatalf("found=%v err=%v", found, err)
	}
	if c.Ready != 3 || c.Busy != 1 || c.Age < 19*time.Second || c.Age > 40*time.Second {
		t.Fatalf("capacity = %+v, want ready 3, busy 1, age about 20s", c)
	}
}
