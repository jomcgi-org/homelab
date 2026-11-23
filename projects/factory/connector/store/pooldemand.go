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
func (s *Store) PendingAttempts(ctx context.Context) ([]AgentAttempts, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT kagent_namespace, kagent_agent, count(*)
		FROM delivery_attempt
		WHERE state IN ('sending', 'ambiguous')
		GROUP BY kagent_namespace, kagent_agent`)
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
