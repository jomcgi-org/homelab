package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// taskMetadata reads the exact accepted task under the caller pinned by
// guardCaller. Each poll is journaled before sending under one stable message
// ID across retries and writer takeovers. Reads always fetch fresh lifecycle
// evidence; a failed read retains execution ownership.
func (e *Engine) taskMetadata(ctx context.Context, a *store.Attempt) (*kagent.Task, error) {
	if a.TaskID == "" || a.ContextID == "" {
		return nil, errors.New("task metadata: missing task or context identity")
	}
	ref := kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}
	messageID := fmt.Sprintf("task-metadata/%d", a.ID)
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.RecordTaskMetadataRead(ctx, tx, a, e.w.Epoch, messageID)
	}); err != nil {
		return nil, err
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	ctx = kagent.WithRequestID(ctx, messageID)
	task, err := e.tasks.GetTaskMetadata(ctx, ref, a.TaskID)
	if err != nil {
		return nil, fmt.Errorf("task metadata: %w", err)
	}
	if task == nil || task.ID != a.TaskID || task.ContextID != a.ContextID || task.Status.State == "" {
		return nil, errors.New("task metadata: invalid task identity or state")
	}
	return task, nil
}

const boundedOutput = "Task output exceeds the connector's 32 MiB transcript budget; transcript omitted."

// outputLimited records the condition once without releasing execution
// ownership or retrying the turn. Only quiescent metadata permits finishing;
// cancellation continues independently through taskMetadata.
func (e *Engine) outputLimited(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt, task *kagent.Task) error {
	if pending, err := e.st.CancelPending(ctx, a.ID); err != nil {
		return err
	} else if pending {
		if !kagent.Quiescent(task.Status.State) {
			return nil
		}
		// A turn that ended before an unsent lease cancellation keeps its
		// outcome, just as it does on the ordinary transcript path.
		if ended, err := e.leaseYields(ctx, r, wi, a); err != nil || !ended {
			return err
		}
	}
	class := Classify(task.Status.State, StatusText(task))
	grantFailure := task.Status.State != kagent.StateCompleted && task.Status.State != kagent.StateInputRequired &&
		(class == ClassCredential || class == ClassQuota)
	if err := e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
		if err := e.st.HoldAttempt(ctx, tx, a.ID, boundedOutput); err != nil {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/output-limit/%d", a.ID), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf("The factory cannot collect output for %s run %d attempt %d. %s Lifecycle polling and cancellation remain active; the slot is retained until the task stops.", wi.IssueKey, r.ID, a.Seq, boundedOutput)}); err != nil {
			return err
		}
		if !kagent.Quiescent(task.Status.State) || grantFailure {
			return nil
		}
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "bounded_output", boundedOutput); err != nil {
			return err
		}
		return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: boundedOutput})
	}); err != nil {
		return err
	}
	if kagent.Quiescent(task.Status.State) && grantFailure {
		// Keep grant failures on their normal blocking path without collecting
		// the transcript or spending an engineering retry.
		e.checked.Delete(a.ID)
		return e.complete(ctx, r, wi, a, task)
	}
	if kagent.Quiescent(task.Status.State) {
		e.checked.Delete(a.ID)
		return nil
	}
	return e.watchSession(ctx, r, wi, a)
}
