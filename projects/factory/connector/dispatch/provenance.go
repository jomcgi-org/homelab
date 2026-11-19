package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"

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

func (e *Engine) prepareProvenance(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) (string, error) {
	text := taskText(r, wi, b, a)
	// Runtime is mandatory in production config. Old admitted runs retain NULL
	// provenance; isolated engine fixtures may omit the runtime dependency.
	if !r.ProvenanceRequired || b.Runtime == nil {
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
	var sessionBase string
	var err error
	if a.ContextID != "" {
		runtime, sessionBase, err = e.st.SessionRuntime(ctx, a.KagentNamespace, a.ContextID, runtime, len(b.Arms) > 0)
		if errors.Is(err, sql.ErrNoRows) {
			err = e.verifyLegacyRuntime(ctx, r, wi, a)
		}
		if err != nil {
			return "", err
		}
	}
	if a.RenderedPrompt != "" {
		if provenance.New(runtime, a.Provenance.BaseSHA, a.RenderedPrompt, a.Provenance.TaskClass) != a.Provenance {
			return "", errRuntimeChanged
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
	var h *github.HTTPError
	if errors.Is(err, github.ErrNotFound) || (errors.As(err, &h) && h.Status == 404) {
		switch {
		case a.Kind == "continuation" && sessionBase != "":
			// A verified session can fail before its first push. Its accepted
			// receipt fixes the starting commit even if main has moved on.
			base, err = sessionBase, nil
		case !r.Connector() && wi.SessionID == "":
			if err := e.beginAdmissionRead(ctx, r, "provenance-base", b.repo()+"/"+b.BaseBranch); err != nil {
				return "", err
			}
			base, err = gh.BranchHead(ctx, b.repo(), b.BaseBranch)
		}
	}
	if err != nil {
		return "", err
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
	p := provenance.New(runtime, base, text, class)
	if err := e.st.RecordProvenance(ctx, e.w, a, p, runtime.SystemPrompt, text); err != nil {
		return "", err
	}
	return a.RenderedPrompt, nil
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
func (e *Engine) replaceRuntimeAttempt(ctx context.Context, r *store.Run, wi *store.WorkItem, b *Binding, a *store.Attempt) error {
	if a.State != store.AttemptSending {
		return store.ErrAttemptChanged
	}
	var next *store.Attempt
	err := e.st.AttemptTx(ctx, e.w, r, wi, a, func(tx *sql.Tx) error {
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, "runtime_changed", errRuntimeChanged.Error()); err != nil {
			return err
		}
		var err error
		next, err = e.record(ctx, tx, r, wi, a.Kind, fmt.Sprintf("runtime-after-%d", a.ID), a.Context)
		return err
	})
	if err != nil {
		return err
	}
	return e.issue(ctx, r, wi, b, next)
}
