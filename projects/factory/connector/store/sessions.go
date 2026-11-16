package store

import (
	"context"
	"time"
)

// Why a session is finished. The values bound the connector's metric label.
const (
	SessionDone     = "done"     // its work item merged
	SessionStopped  = "stopped"  // its work item stopped and no run resumed it
	SessionReplaced = "replaced" // its work item no longer records it
)

// FinishedSession is a kagent session of a work item that no open attempt,
// pending cancellation or recovery hold uses. Current is true when the work
// item still records it: ReleaseSession must clear that before the session
// is deleted.
type FinishedSession struct {
	ContextID       string
	WorkItemID      int64
	IssueKey        string
	KagentNamespace string
	KagentAgent     string
	Reason          string
	Current         bool
}

// FinishedSessions lists the binding's finished sessions:
//   - the session of a done work item;
//   - the session of a work item stopped for stoppedKeep, the time a run
//     that resumes it may still use it;
//   - every session a work item no longer records.
//
// A session whose last attempt finished within quiet, or whose work item
// has an open run (only for a session the work item still records), is not
// listed. Times are the database's.
func (s *Store) FinishedSessions(ctx context.Context, binding string, stoppedKeep, quiet time.Duration) ([]FinishedSession, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT DISTINCT ON (a.context_id) a.context_id, w.id, w.issue_key, a.kagent_namespace, a.kagent_agent,
			CASE WHEN a.context_id = w.session_id OR (w.session_id IS NULL AND w.state IN ('done', 'stopped'))
				THEN w.state ELSE 'replaced' END,
			(a.context_id = w.session_id) IS TRUE
		FROM delivery_attempt a
		JOIN run r ON r.id = a.run_id
		JOIN work_item w ON w.id = r.work_item_id
		WHERE w.binding_name = $1 AND a.context_id IS NOT NULL
			AND (a.context_id IS DISTINCT FROM w.session_id OR w.state = 'done'
				OR (w.state = 'stopped' AND w.updated_at < now() - $2::float8 * interval '1 second'))
			AND NOT EXISTS (SELECT 1 FROM delivery_attempt o WHERE o.context_id = a.context_id
				AND (o.state IN ('sending', 'ambiguous', 'accepted')
					OR o.finished_at > now() - $3::float8 * interval '1 second'))
			AND NOT EXISTS (SELECT 1 FROM cancellation c JOIN delivery_attempt o ON o.id = c.delivery_attempt_id
				WHERE o.context_id = a.context_id AND c.state = 'pending')
			AND NOT EXISTS (SELECT 1 FROM recovery_hold h
				WHERE h.work_item_id = w.id AND h.state IN ('pending', 'attested'))
			AND (a.context_id IS DISTINCT FROM w.session_id OR NOT EXISTS (SELECT 1 FROM run o
				WHERE o.state <> 'closed' AND (o.work_item_id = w.id OR o.issue_id = w.issue_id)))
		ORDER BY a.context_id`, binding, stoppedKeep.Seconds(), quiet.Seconds())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []FinishedSession
	for rows.Next() {
		var f FinishedSession
		if err := rows.Scan(&f.ContextID, &f.WorkItemID, &f.IssueKey, &f.KagentNamespace, &f.KagentAgent, &f.Reason, &f.Current); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// ReleaseSession clears a done or stopped work item's session, so a run
// that resumes the work item opens a new one with the issue brief, as after
// a replaced session. It does nothing (false) when the work item no longer
// records contextID or a run, attempt or recovery hold may still use it.
// The caller deletes the session afterwards; a failed delete is retried as a
// session the work item no longer records.
func (s *Store) ReleaseSession(ctx context.Context, q Querier, workID int64, contextID string) (bool, error) {
	res, err := q.ExecContext(ctx, `
		UPDATE work_item w SET session_id = NULL, updated_at = now()
		WHERE w.id = $1 AND w.session_id = $2 AND w.state IN ('done', 'stopped')
			AND NOT EXISTS (SELECT 1 FROM run o
				WHERE o.state <> 'closed' AND (o.work_item_id = w.id OR o.issue_id = w.issue_id))
			AND NOT EXISTS (SELECT 1 FROM delivery_attempt a
				WHERE a.context_id = $2 AND a.state IN ('sending', 'ambiguous', 'accepted'))
			AND NOT EXISTS (SELECT 1 FROM recovery_hold h
				WHERE h.work_item_id = w.id AND h.state IN ('pending', 'attested'))`, workID, contextID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}
