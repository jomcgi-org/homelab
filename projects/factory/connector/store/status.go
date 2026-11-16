package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/lib/pq"
)

// RecordStatusRead checks the epoch and commits the attempt on the writer's
// advisory-lock connection. A retry updates one identity instead of creating
// another operation. Read results are never deduplicated or stored here.
func (w *Writer) RecordStatusRead(ctx context.Context, messageID string) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO status_read (message_id, writer_epoch) VALUES ($1, $2)
			ON CONFLICT (message_id) DO UPDATE SET attempts = status_read.attempts + 1,
				writer_epoch = EXCLUDED.writer_epoch, attempted_at = now()`, messageID, w.Epoch)
		return err
	})
}

// StatusItem is the store's body-free projection for the landing page.
type StatusItem struct {
	BindingName  string
	IssueID      int64
	Key          string
	State        string
	ReviewRounds int
	PRNumber     int
	PRURL        string
	MergedAt     *time.Time
}

// StatusItems bounds reads to 100 live work items and the ten latest merges
// across the configured bindings. It never reads runs, sessions or outboxes.
func (s *Store) StatusItems(ctx context.Context, bindings []string) ([]StatusItem, error) {
	rows, err := s.DB.QueryContext(ctx, `
		WITH live AS (
			SELECT w.binding_name, w.issue_id, w.issue_key, w.state, w.review_rounds,
				coalesce(p.number, 0) AS number, coalesce(p.url, '') AS url, NULL::timestamptz AS merged_at
			FROM work_item w LEFT JOIN pull_request p ON p.work_item_id = w.id
			WHERE w.binding_name = ANY($1) AND w.state IN ('active', 'blocked', 'review', 'merging')
				AND (p.state IS NULL OR p.state <> 'merged')
			ORDER BY w.id LIMIT 101
		), shipped AS (
			SELECT w.binding_name, w.issue_id, w.issue_key, w.state, w.review_rounds,
				p.number, p.url, p.merged_at
			FROM work_item w JOIN pull_request p ON p.work_item_id = w.id
			WHERE w.binding_name = ANY($1) AND p.state = 'merged' AND p.merged_at IS NOT NULL
			ORDER BY p.merged_at DESC, p.id DESC LIMIT 10
		)
		SELECT * FROM live UNION ALL SELECT * FROM shipped
		ORDER BY merged_at DESC NULLS LAST, issue_key`, pq.Array(bindings))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StatusItem
	live := 0
	for rows.Next() {
		var item StatusItem
		if err := rows.Scan(&item.BindingName, &item.IssueID, &item.Key, &item.State, &item.ReviewRounds,
			&item.PRNumber, &item.PRURL, &item.MergedAt); err != nil {
			return nil, err
		}
		if item.MergedAt == nil {
			live++
			if live > 100 {
				return nil, fmt.Errorf("store: active work listing reached its limit")
			}
		}
		out = append(out, item)
	}
	return out, rows.Err()
}
