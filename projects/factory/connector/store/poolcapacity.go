package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// PoolCapacity is the worker reaper's last count of one WorkerPool's workers.
type PoolCapacity struct {
	Ready int
	Busy  int
	// Age is how long ago the reaper wrote the row, by the database's clock.
	Age time.Duration
}

// WorkerPoolCapacity reads one pool's capacity. found is false when the
// reaper has never written the pool (it is not configured for it, or the
// pool has no autoscaler).
func (s *Store) WorkerPoolCapacity(ctx context.Context, namespace, pool string) (c PoolCapacity, found bool, err error) {
	var age float64
	err = s.DB.QueryRowContext(ctx, `
		SELECT ready, busy, extract(epoch FROM now() - observed_at)
		FROM worker_pool_capacity WHERE namespace = $1 AND pool = $2`, namespace, pool).Scan(&c.Ready, &c.Busy, &age)
	if errors.Is(err, sql.ErrNoRows) {
		return PoolCapacity{}, false, nil
	}
	if err != nil {
		return PoolCapacity{}, false, err
	}
	c.Age = time.Duration(age * float64(time.Second))
	return c, true, nil
}
