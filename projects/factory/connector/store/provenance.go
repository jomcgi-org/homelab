package store

import (
	"context"
	"database/sql"

	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

// RecordProvenance saves the exact sent text with the attempt and the first
// execution inputs with the run, under the existing writer fence. A proposed
// run snapshot may change only while every send is definitively unaccepted;
// MarkIssued makes it immutable before execution or an ambiguous response.
//
// invariant: FACT-17
func (s *Store) RecordProvenance(ctx context.Context, w *Writer, a *Attempt, p provenance.Record, system, text string) error {
	if err := p.Validate(); err != nil {
		return err
	}
	return w.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `UPDATE run SET model=$2, reasoning_effort=$3, arm=$4,
   base_sha=$5, prompt_hash=$6, task_class=$7, guidance_system_prompt=$8
   WHERE id=$1 AND provenance_required AND (model IS NULL OR NOT EXISTS (
     SELECT 1 FROM delivery_attempt WHERE run_id=$1 AND execution_possible))`, a.RunID,
			p.Model, p.ReasoningEffort, p.Arm, p.BaseSHA, p.PromptHash, p.TaskClass, system); err != nil {
			return err
		}
		return tx.QueryRowContext(ctx, `UPDATE delivery_attempt SET rendered_prompt=$2,
   model=$3,reasoning_effort=$4,arm=$5,base_sha=$6,prompt_hash=$7,task_class=$8,guidance_system_prompt=$9
   WHERE id=$1 AND rendered_prompt IS NULL RETURNING rendered_prompt,model,reasoning_effort,arm,base_sha,prompt_hash,task_class`,
			a.ID, text, p.Model, p.ReasoningEffort, p.Arm, p.BaseSHA, p.PromptHash, p.TaskClass, system).
			Scan(&a.RenderedPrompt, &a.Provenance.Model, &a.Provenance.ReasoningEffort, &a.Provenance.Arm,
				&a.Provenance.BaseSHA, &a.Provenance.PromptHash, &a.Provenance.TaskClass)
	})
}

// SessionRuntime retains the model, system prompt and starting commit of a
// known session's first accepted dispatch. The commit also covers a retry
// before that session publishes its work branch.
// sql.ErrNoRows means the caller must verify the legacy session revision.
// Arm-specific URLs pin effort in the session model configuration. Sessions
// using the shared relay endpoint follow the current broker default.
func (s *Store) SessionRuntime(ctx context.Context, namespace, session string, configured provenance.Runtime, pinEffort bool) (provenance.Runtime, string, error) {
	r := configured
	var base, effort string
	err := s.DB.QueryRowContext(ctx, `SELECT a.model,a.arm,a.guidance_system_prompt,a.base_sha,a.reasoning_effort FROM run r
  JOIN delivery_attempt a ON a.run_id=r.id
  WHERE a.kagent_namespace=$1 AND a.context_id=$2 AND a.model IS NOT NULL AND a.task_id IS NOT NULL
  ORDER BY a.id LIMIT 1`, namespace, session).Scan(&r.Model, &r.Arm, &r.SystemPrompt, &base, &effort)
	if pinEffort && err == nil {
		r.ReasoningEffort = effort
	}
	return r, base, err
}
