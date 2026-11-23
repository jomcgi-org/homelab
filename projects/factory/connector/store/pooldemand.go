package store

import "context"

// AgentAttempts is the number of open delivery attempts of one kagent Agent.
type AgentAttempts struct {
	Namespace string
	Agent     string
	Attempts  int64
}

// PendingAttempts counts, by kagent Agent, the attempts that need an actor and
// have no accepted task: sending (recorded, not sent) and ambiguous (sent,
// outcome unknown). The attempt row commits before the send, so the count
// covers a send that has not left the connector yet.
//
// Only attempts of a live run count: dispatched and not stopped. A run can
// close without finishing its attempt (for example when its binding is no
// longer configured), and such an attempt would otherwise count as demand for
// good and keep a pool from scaling down.
func (s *Store) PendingAttempts(ctx context.Context) ([]AgentAttempts, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT a.kagent_namespace, a.kagent_agent, count(*)
		FROM delivery_attempt a
		JOIN run r ON r.id = a.run_id
		WHERE a.state IN ('sending', 'ambiguous')
			AND r.state = 'dispatched' AND r.stopped_reason IS NULL
		GROUP BY a.kagent_namespace, a.kagent_agent`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AgentAttempts
	for rows.Next() {
		var a AgentAttempts
		if err := rows.Scan(&a.Namespace, &a.Agent, &a.Attempts); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
