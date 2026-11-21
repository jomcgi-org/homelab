package store

import (
	"context"
	"database/sql"

	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

// RecordProvenance saves the exact sent text and the analysis main commit
// with the attempt. The run retains its first execution inputs. MarkIssued
// makes the run snapshot immutable before execution or an ambiguous response.
func (s *Store) RecordProvenance(ctx context.Context, w *Writer, a *Attempt, p provenance.Record, system, text, analysisMain string) error {
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
   model=$3,reasoning_effort=$4,arm=$5,base_sha=$6,prompt_hash=$7,task_class=$8,guidance_system_prompt=$9,analysis_main_sha=nullif($10,'')
   WHERE id=$1 AND rendered_prompt IS NULL
   RETURNING rendered_prompt,model,reasoning_effort,arm,base_sha,prompt_hash,task_class,coalesce(analysis_main_sha,'')`,
			a.ID, text, p.Model, p.ReasoningEffort, p.Arm, p.BaseSHA, p.PromptHash, p.TaskClass, system, analysisMain).
			Scan(&a.RenderedPrompt, &a.Provenance.Model, &a.Provenance.ReasoningEffort, &a.Provenance.Arm,
				&a.Provenance.BaseSHA, &a.Provenance.PromptHash, &a.Provenance.TaskClass, &a.AnalysisMainSHA)
	})
}

// AnalysisTask is the accepted coder task that owns a session's analysis pin.
type AnalysisTask struct {
	AttemptID      int64
	Agent          string
	ContextID      string
	TaskID         string
	MainSHA        string
	CallerIdentity string
}

// CurrentAnalysisTask finds a unique accepted receipt for a verified actor
// session. The caller also checks that kagent still reports the exact task
// as working before it hands the main commit to an actor.
func (s *Store) CurrentAnalysisTask(ctx context.Context, namespace, session string) (AnalysisTask, error) {
	var task AnalysisTask
	err := s.DB.QueryRowContext(ctx, `
		WITH matching AS (
			SELECT a.id, a.kagent_agent, a.context_id, a.task_id, a.analysis_main_sha, w.caller_identity
			FROM delivery_attempt a
			JOIN run r ON r.id = a.run_id
			JOIN work_item w ON w.id = r.work_item_id
			WHERE a.kagent_namespace = $1 AND lower(a.context_id) = $2
				AND a.state = 'accepted' AND a.task_id <> ''
				AND a.analysis_main_sha IS NOT NULL
				AND nullif(w.caller_identity,'') IS NOT NULL
				AND r.state = 'dispatched' AND r.stopped_reason IS NULL
				AND r.attempt = a.run_attempt AND w.epoch = a.work_item_epoch
				AND w.session_id = a.context_id AND w.kagent_namespace = a.kagent_namespace
				AND NOT EXISTS (
					SELECT 1 FROM cancellation c
					WHERE c.delivery_attempt_id = a.id AND c.state = 'pending'
				)
			LIMIT 2
		)
		SELECT id, kagent_agent, context_id, task_id, analysis_main_sha, caller_identity FROM matching
		WHERE (SELECT count(*) FROM matching) = 1`, namespace, session).
		Scan(&task.AttemptID, &task.Agent, &task.ContextID, &task.TaskID, &task.MainSHA, &task.CallerIdentity)
	return task, err
}

// SessionCommits holds the two commits in a session's first accepted dispatch.
type SessionCommits struct {
	Checkout     string
	AnalysisMain string
}

// SessionRuntime retains the model, system prompt and commits of a
// known session's first accepted dispatch. The commits also cover a retry
// before that session publishes its work branch.
// sql.ErrNoRows means the caller must verify the legacy session revision.
// Arm-specific URLs pin effort in the session model configuration. Sessions
// using the shared relay endpoint follow the current broker default.
func (s *Store) SessionRuntime(ctx context.Context, namespace, session string, configured provenance.Runtime, pinEffort bool) (provenance.Runtime, SessionCommits, error) {
	r := configured
	var commits SessionCommits
	var effort string
	err := s.DB.QueryRowContext(ctx, `SELECT a.model,a.arm,a.guidance_system_prompt,a.base_sha,
     coalesce(a.analysis_main_sha,''),a.reasoning_effort FROM run r
  JOIN delivery_attempt a ON a.run_id=r.id
  WHERE a.kagent_namespace=$1 AND a.context_id=$2 AND a.model IS NOT NULL AND a.task_id IS NOT NULL
  ORDER BY a.id LIMIT 1`, namespace, session).Scan(&r.Model, &r.Arm, &r.SystemPrompt, &commits.Checkout, &commits.AnalysisMain, &effort)
	if pinEffort && err == nil {
		r.ReasoningEffort = effort
	}
	return r, commits, err
}
