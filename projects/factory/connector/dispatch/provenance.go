package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type branchReader interface {
	BranchHead(context.Context, string, string) (string, error)
}

var (
	errLegacyRuntime  = errors.New("legacy session requires fenced replacement for verified provenance")
	errRuntimeChanged = errors.New("unaccepted attempt runtime changed")
)

// ResolveAnalysisMain returns the canonical main commit for the coder task
// currently working in a verified actor session.
func (e *Engine) ResolveAnalysisMain(ctx context.Context, namespace, session string) (string, error) {
	assigned, err := e.st.CurrentAnalysisTask(ctx, namespace, session)
	if err != nil {
		return "", err
	}
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	if e.cfg.Caller == nil {
		return "", errCallerUncertain
	}
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() || identity.Key() != assigned.CallerIdentity {
		return "", callerUncertain(err)
	}
	task, err := e.taskMetadata(ctx, &store.Attempt{
		ID: assigned.AttemptID, KagentNamespace: namespace, KagentAgent: assigned.Agent,
		ContextID: assigned.ContextID, TaskID: assigned.TaskID,
	})
	if err != nil {
		return "", err
	}
	if task.Status.State != kagent.StateWorking {
		return "", sql.ErrNoRows
	}
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	current, err := e.st.CurrentAnalysisTask(ctx, namespace, session)
	if err != nil {
		return "", err
	}
	if current != assigned {
		return "", sql.ErrNoRows
	}
	return current.MainSHA, nil
}

func (e *Engine) prepareProvenance(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt, sendContext *string) (string, error) {
	text := taskText(r, wi, b, a)
	// Runtime is mandatory in production. The store checks run eligibility
	// separately when it records the attempt.
	if b.Runtime == nil {
		return text, nil
	}
	runtime := *b.Runtime
	if !wi.Escalated && wi.FirstArm != "" && wi.FirstArm != "control" {
		found := false
		for _, arm := range b.Arms {
			if arm.Name == wi.FirstArm {
				runtime.Arm, runtime.Model, runtime.ReasoningEffort = arm.Name, arm.Model, arm.ReasoningEffort
				found = true
				break
			}
		}
		if !found {
			return "", fmt.Errorf("assigned coder arm %q missing from binding", wi.FirstArm)
		}
	}
	var sessionCommits store.SessionCommits
	var err error
	verify := a.ContextID == ""
	if a.ContextID != "" {
		runtime, sessionCommits, err = e.st.SessionRuntime(ctx, a.KagentNamespace, a.ContextID, runtime, len(b.Arms) > 0)
		if errors.Is(err, sql.ErrNoRows) {
			verify = true
			err = e.verifyLegacyRuntime(ctx, r, wi, a)
		}
		if err != nil {
			return "", err
		}
	}
	if verify {
		if err := e.beginAdmissionRead(ctx, r, "provenance-prompt", a.MessageID); err != nil {
			return "", err
		}
		reader := kagent.PromptSessionReader{
			PromptRevision: e.sessions.PromptRevision,
			GetSession: func(ctx context.Context, id string) (*kagent.Session, error) {
				if err := e.beginAdmissionRead(ctx, r, "provenance-pinned-session", id); err != nil {
					return nil, err
				}
				return e.sessions.GetSession(ctx, id)
			},
		}
		*sendContext, err = kagent.PinPromptSession(ctx, reader, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.MessageID, a.ContextID, runtime.SystemPrompt, func(ctx context.Context, agent kagent.AgentRef, key string) (*kagent.Session, error) {
			if err := e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
				return e.st.RecordAdmissionRead(ctx, tx, r, e.w.Epoch, key, "create-prompt-session", key)
			}); err != nil {
				return nil, err
			}
			if err := e.guard(ctx); err != nil {
				return nil, err
			}
			return e.sessions.CreateSession(ctx, agent, key)
		})
		if err != nil {
			return "", err
		}
	}
	if a.RenderedPrompt != "" {
		if provenance.New(runtime, a.Provenance.BaseSHA, a.RenderedPrompt, a.Provenance.TaskClass) != a.Provenance {
			return "", errRuntimeChanged
		}
		if a.AnalysisMainSHA == "" {
			main, err := e.readAnalysisMain(ctx, r, b)
			if err != nil {
				return "", err
			}
			if err := e.st.RecordUnissuedAnalysisMain(ctx, e.w, a, main); err != nil {
				return "", err
			}
		}
		return a.RenderedPrompt, nil
	}
	gh, ok := e.gh.(branchReader)
	if !ok {
		return "", errors.New("run provenance requires a branch reader")
	}
	if err := e.beginAdmissionRead(ctx, r, "provenance-branch", b.repo()+"/"+wi.Branch); err != nil {
		return "", err
	}
	base, err := gh.BranchHead(ctx, b.repo(), wi.Branch)
	var analysisMain string
	if err == nil && wi.Branch == b.BaseBranch {
		analysisMain = base
	}
	var h *github.HTTPError
	if errors.Is(err, github.ErrNotFound) || (errors.As(err, &h) && h.Status == 404) {
		switch {
		case a.Kind == "continuation" && sessionCommits.Checkout != "":
			// A verified session can fail before its first push. Its accepted
			// receipt fixes both commits even if main has moved on.
			base, analysisMain, err = sessionCommits.Checkout, sessionCommits.AnalysisMain, nil
		case a.Kind == "continuation" && a.ContextID != "":
			// A matching Agent revision cannot establish the old checkout SHA.
			return "", errLegacyRuntime
		case !r.Connector() && wi.SessionID == "":
			if err := e.beginAdmissionRead(ctx, r, "provenance-base", b.repo()+"/"+b.BaseBranch); err != nil {
				return "", err
			}
			base, err = gh.BranchHead(ctx, b.repo(), b.BaseBranch)
			analysisMain = base
		}
	}
	if err != nil {
		return "", err
	}
	if analysisMain == "" {
		analysisMain, err = e.readAnalysisMain(ctx, r, b)
		if err != nil {
			return "", err
		}
	}
	if !baseCommit.MatchString(analysisMain) {
		return "", errors.New("analysis main commit is missing or invalid")
	}
	issue := r.AdmissionIssue
	if issue == nil {
		if err := e.beginAdmissionRead(ctx, r, "provenance-issue", strconv.FormatInt(wi.IssueID, 10)); err != nil {
			return "", err
		}
		issue, err = e.its.GetIssue(ctx, wi.IssueID)
		if err != nil {
			return "", err
		}
	}
	if issue == nil || issue.ID != wi.IssueID {
		return "", errors.New("run provenance issue unavailable")
	}
	var project *itsaplan.Project
	if len(issue.LabelIDs) > 0 {
		if err := e.beginAdmissionRead(ctx, r, "provenance-labels", b.ItsaplanProject); err != nil {
			return "", err
		}
		project, err = e.its.GetProject(ctx, b.ItsaplanProject)
		if err != nil {
			return "", err
		}
	}
	class, err := provenance.TaskClass(issue, project)
	if err != nil {
		return "", err
	}
	text += fmt.Sprintf("\nWorktree starting commit: %s\nStart the worktree at this exact commit before editing. Preserve the assigned work branch name and any uncommitted work.\n", base)
	text += "\nImmediately after checkout and before editing, run this exact standalone shell command in the assigned worktree so the harness records its output:\n" + checkoutProbe + "\n"

	p := provenance.New(runtime, base, text, class)
	if err := e.st.RecordProvenance(ctx, e.w, a, p, runtime.SystemPrompt, text, analysisMain); err != nil {
		return "", err
	}
	return a.RenderedPrompt, nil
}

func (e *Engine) readAnalysisMain(ctx context.Context, r *store.Run, b *Binding) (string, error) {
	gh, ok := e.gh.(branchReader)
	if !ok {
		return "", errors.New("run provenance requires a branch reader")
	}
	if err := e.beginAdmissionRead(ctx, r, "provenance-analysis-main", b.repo()+"/"+b.BaseBranch); err != nil {
		return "", err
	}
	main, err := gh.BranchHead(ctx, b.repo(), b.BaseBranch)
	var refused *github.HTTPError
	if errors.As(err, &refused) && (refused.Status == 401 || refused.Status == 403) {
		return "", &github.HTTPError{Status: refused.Status}
	}
	if err == nil && !baseCommit.MatchString(main) {
		err = errors.New("analysis main commit is missing or invalid")
	}
	return main, err
}

// A pre-migration session has no recorded model. Only use current settings
// when kagent proves that session is pinned to the current Agent revision.
func (e *Engine) verifyLegacyRuntime(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	reader, ok := e.sessions.(interface {
		AgentRevision(context.Context, kagent.AgentRef) (string, error)
	})
	if !ok {
		return errors.New("legacy session runtime cannot be verified")
	}
	if err := e.beginAdmissionRead(ctx, r, "provenance-session", a.ContextID); err != nil {
		return err
	}
	session, err := e.sessions.GetSession(ctx, a.ContextID)
	if err != nil {
		return err
	}
	if err := e.beginAdmissionRead(ctx, r, "provenance-agent", wi.KagentNamespace+"/"+wi.KagentAgent); err != nil {
		return err
	}
	revision, err := reader.AgentRevision(ctx, agentRef(wi))
	if err != nil {
		return err
	}
	if session == nil || revision == "" || session.PreparedRevision != revision {
		return errLegacyRuntime
	}
	return nil
}

// A ready attempt has never been accepted. Retire its immutable receipt and
// allocate a new message when runtime inputs change. AttemptTx fences stops,
// cancellation, reassignment and writer takeover; ambiguous calls never enter.
func (e *Engine) replaceRuntimeAttempt(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt, cause error) error {
	if a.State != store.AttemptSending {
		return store.ErrAttemptChanged
	}
	var next *store.Attempt
	err := e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "runtime_changed", cause.Error()); err != nil {
			return err
		}
		var err error
		next, err = e.record(ctx, tx, r, wi, a.Kind, fmt.Sprintf("runtime-after-%d", a.ID), a.Context)
		return err
	})
	if err != nil {
		return err
	}
	if errors.Is(cause, kagent.ErrCreatedRevisionMismatch) {
		return nil
	} // Retry the replacement on the next poll.
	return e.issue(ctx, r, wi, b, next)
}

const checkoutProbe = "git symbolic-ref --short HEAD && git rev-parse --verify HEAD"

// invariant: HOME-404-1
func observedStart(t *kagent.Task, branch string) string {
	calls := map[string]bool{}
	for _, artifact := range t.Artifacts {
		if artifact == nil {
			continue
		}
		for _, part := range artifact.Parts {
			data, _ := part.Data.(map[string]any)
			id, _ := data["id"].(string)
			if id == "" || data["name"] != "command_execution" {
				continue
			}
			switch part.Metadata[kagent.PartTypeKey] {
			case kagent.PartToolCall:
				args, _ := data["args"].(map[string]any)
				command, _ := args["command"].(string)
				calls[id] = command == checkoutProbe || command == "/bin/bash -lc '"+checkoutProbe+"'" || command == "/bin/sh -c '"+checkoutProbe+"'"
			case kagent.PartToolResponse:
				if !calls[id] {
					continue
				}
				delete(calls, id)
				response, _ := data["response"].(map[string]any)
				result, _ := response["result"].(map[string]any)
				output, _ := result["output"].(string)
				ref, sha, ok := strings.Cut(strings.TrimSpace(output), "\n")
				if result["status"] == "completed" && result["exitCode"] == float64(0) && ok && ref == branch && baseCommit.MatchString(sha) {
					return sha
				}
			}
		}
	}
	return ""
}
