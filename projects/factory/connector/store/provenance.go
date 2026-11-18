package store

import (
	"context"
	"database/sql"

	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

// RecordProvenance saves the exact sent text with the attempt and the first
// execution inputs with the run, under the existing writer fence.
func (s *Store) RecordProvenance(ctx context.Context, w *Writer, a *Attempt, p provenance.Record, system, text string) error {
	if err := p.Validate(); err != nil {
		return err
	}
	return w.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `UPDATE run SET model=$2, reasoning_effort=$3, arm=$4,
   base_sha=$5, prompt_hash=$6, task_class=$7, guidance_system_prompt=$8
   WHERE id=$1 AND model IS NULL AND provenance_required`, a.RunID,
			p.Model, p.ReasoningEffort, p.Arm, p.BaseSHA, p.PromptHash, p.TaskClass, system); err != nil {
			return err
		}
		return tx.QueryRowContext(ctx, `UPDATE delivery_attempt SET rendered_prompt=coalesce(rendered_prompt,$2)
   WHERE id=$1 RETURNING rendered_prompt`, a.ID, text).Scan(&a.RenderedPrompt)
	})
}

// SessionRuntime retains the model and system prompt pinned to a known session.
// sql.ErrNoRows means the caller must verify the legacy session revision.
// Effort is enforced by the broker on each call, so the current setting wins.
func (s *Store) SessionRuntime(ctx context.Context, namespace, session string, configured provenance.Runtime) (provenance.Runtime, error) {
	r := configured
	err := s.DB.QueryRowContext(ctx, `SELECT r.model,r.arm,r.guidance_system_prompt FROM run r
  JOIN delivery_attempt a ON a.run_id=r.id
  WHERE a.kagent_namespace=$1 AND a.context_id=$2 AND r.model IS NOT NULL
  ORDER BY a.id LIMIT 1`, namespace, session).Scan(&r.Model, &r.Arm, &r.SystemPrompt)
	return r, err
}
