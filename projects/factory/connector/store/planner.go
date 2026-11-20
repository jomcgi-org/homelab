package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/jomcgi/homelab/projects/factory/connector/planner"
	"github.com/lib/pq"
)

// SyncPlannerSchedules applies the reviewed set as one writer transaction.
// Removed schedules keep their runs, reservations and breaker history.
func (w *Writer) SyncPlannerSchedules(ctx context.Context, schedules []planner.Schedule) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		names := make([]string, 0, len(schedules))
		for _, s := range schedules {
			_, err := tx.ExecContext(ctx, `INSERT INTO planner_schedule
				(name, namespace, ceiling, min_interval, cooldown, per_run, issue_title, instructions, enabled)
				VALUES ($1, $2, $3, $4::bigint * interval '1 microsecond',
					$5::bigint * interval '1 microsecond', $6, $7, $8, true)
				ON CONFLICT (name) DO UPDATE SET namespace = EXCLUDED.namespace,
					ceiling = EXCLUDED.ceiling, min_interval = EXCLUDED.min_interval,
					cooldown = EXCLUDED.cooldown, per_run = EXCLUDED.per_run,
					issue_title = EXCLUDED.issue_title, instructions = EXCLUDED.instructions, enabled = true`,
				s.Name, s.Namespace, s.EffectiveCeiling(), s.MinInterval.Microseconds(),
				s.Cooldown.Microseconds(), s.PerRun, s.IssueTitle, s.Instructions)
			if err != nil {
				return fmt.Errorf("upsert planner schedule %q: %w", s.Name, err)
			}
			names = append(names, s.Name)
		}
		_, err := tx.ExecContext(ctx, `UPDATE planner_schedule SET enabled = false
			WHERE enabled AND NOT (name = ANY($1::text[]))`, pq.Array(names))
		return err
	})
}
