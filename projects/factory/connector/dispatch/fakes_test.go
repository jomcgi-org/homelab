package dispatch_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// fakeItsaplan models the v1.10.0 runner semantics the connector relies on:
// leases, attempts, 404 for a reclaimed or finished attempt, offset-checked
// events and terminal results.
type fakeItsaplan struct {
	mu       sync.Mutex
	now      func() time.Time
	lease    time.Duration
	maxTries int
	runs     []*fakeRun
	comments []fakeComment
	calls    int
	// commentErr fails every comment while set.
	commentErr error
	// resultGone fails the run in itsaplan as its result arrives, as
	// expireExhaustedRuns can.
	resultGone bool
	columns    []itsaplan.Column
	moves      []fakeMove
	issues     map[int64]*itsaplan.Issue
	// moveErr fails the next MoveIssue calls.
	moveErr []error
}

type fakeMove struct {
	issueID, columnID int64
}

type fakeRun struct {
	run      itsaplan.Run
	agent    string
	status   string
	attempts int
	until    time.Time
	hidden   bool
	offsets  map[int]int
	events   map[int][]itsaplan.Event
	results  []itsaplan.Result
	beats    int
}

type fakeComment struct {
	issueID int64
	body    string
}

func newFakeItsaplan(now func() time.Time) *fakeItsaplan {
	return &fakeItsaplan{now: now, lease: 300 * time.Second, maxTries: 3, columns: []itsaplan.Column{
		{ID: 1, Name: "Todo", StateType: "unstarted"},
		{ID: 2, Name: "In progress", StateType: "started", Position: 1},
		{ID: 3, Name: "Done", StateType: "completed", Position: 2},
		{ID: 4, Name: "Canceled", StateType: "canceled", Position: 3},
	}}
}

func (f *fakeItsaplan) add(id int64, agent string, issueID int64, key, prompt string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	iid := issueID
	if f.issues == nil {
		f.issues = map[int64]*itsaplan.Issue{}
	}
	if f.issues[issueID] == nil {
		f.issues[issueID] = &itsaplan.Issue{
			ID: issueID, Identifier: key, ColumnID: 2,
			Title: fmt.Sprintf("Issue %d title", issueID), Description: fmt.Sprintf("Issue %d description.", issueID),
		}
	}
	f.runs = append(f.runs, &fakeRun{
		run: itsaplan.Run{
			ID: id, Trigger: "mention", Prompt: prompt, SystemPrompt: "You are a coding agent.",
			TraceEvents: true, IssueID: &iid, IssueIdentifier: key,
		},
		agent: agent, status: "pending", offsets: map[int]int{}, events: map[int][]itsaplan.Event{},
	})
}

func (f *fakeItsaplan) get(id int64) *fakeRun {
	for _, r := range f.runs {
		if r.run.ID == id {
			return r
		}
	}
	return nil
}

func (f *fakeItsaplan) Run(id int64) fakeRun {
	f.mu.Lock()
	defer f.mu.Unlock()
	return *f.get(id)
}

func (f *fakeItsaplan) Calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

// cancel moves the run's issue to another project: itsaplan cancels the run
// and the queue hides it.
func (f *fakeItsaplan) cancel(id int64, hide bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.get(id)
	r.status = "canceled"
	r.hidden = hide
}

// reclaimElsewhere hands the run's next attempt to another runner.
func (f *fakeItsaplan) reclaimElsewhere(id int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.get(id)
	r.attempts++
	r.until = f.now().Add(f.lease)
}

func (f *fakeItsaplan) Claim(_ context.Context, agent string) (*itsaplan.Run, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	now := f.now()
	for _, r := range f.runs {
		if r.agent != agent || r.status != "pending" || now.Before(r.until) {
			continue
		}
		if r.attempts >= f.maxTries {
			r.status = "failed"
			continue
		}
		r.attempts++
		r.until = now.Add(f.lease)
		out := r.run
		out.Attempts = r.attempts
		return &out, nil
	}
	return nil, nil
}

func (f *fakeItsaplan) Heartbeat(_ context.Context, agent string, id int64, attempt int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	r := f.get(id)
	if r == nil || r.agent != agent || r.status != "pending" || r.attempts != attempt {
		return itsaplan.ErrNotFound
	}
	r.beats++
	r.until = f.now().Add(f.lease)
	return nil
}

func (f *fakeItsaplan) Events(_ context.Context, agent string, id int64, attempt, offset int, events []itsaplan.Event) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	r := f.get(id)
	if r == nil || r.agent != agent || r.status != "pending" || r.attempts != attempt || !f.now().Before(r.until) {
		return 0, itsaplan.ErrNotFound
	}
	next := r.offsets[attempt]
	if offset < next && offset+len(events) <= next {
		return next, nil
	}
	if offset != next {
		return 0, fmt.Errorf("%w: unexpected trace offset", itsaplan.ErrConflict)
	}
	r.events[attempt] = append(r.events[attempt], events...)
	r.offsets[attempt] = offset + len(events)
	return r.offsets[attempt], nil
}

func (f *fakeItsaplan) Result(_ context.Context, agent string, id int64, res itsaplan.Result) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	r := f.get(id)
	if f.resultGone {
		r.status = "failed"
	}
	if r == nil || r.agent != agent || r.status != "pending" || (res.Attempt != 0 && r.attempts != res.Attempt) {
		return itsaplan.ErrNotFound
	}
	r.status = res.Status
	r.results = append(r.results, res)
	return nil
}

func (f *fakeItsaplan) GetRun(_ context.Context, id int64) (*itsaplan.RunStatus, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	r := f.get(id)
	if r == nil || r.hidden {
		return nil, itsaplan.ErrNotFound
	}
	return &itsaplan.RunStatus{ID: id, Status: r.status, Attempts: r.attempts}, nil
}

func (f *fakeItsaplan) Comment(_ context.Context, issueID int64, body string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.commentErr != nil {
		return f.commentErr
	}
	f.comments = append(f.comments, fakeComment{issueID, body})
	return nil
}

func (f *fakeItsaplan) GetIssue(_ context.Context, issueID int64) (*itsaplan.Issue, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	is := f.issues[issueID]
	if is == nil {
		return nil, itsaplan.ErrNotFound
	}
	c := *is
	return &c, nil
}

// moveTo puts an issue in a column, or in another project's.
func (f *fakeItsaplan) moveTo(issueID, columnID int64, identifier string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	is := f.issues[issueID]
	is.ColumnID = columnID
	if identifier != "" {
		is.Identifier = identifier
	}
}

func (f *fakeItsaplan) ProjectColumns(_ context.Context, _ string) ([]itsaplan.Column, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	return append([]itsaplan.Column(nil), f.columns...), nil
}

func (f *fakeItsaplan) MoveIssue(_ context.Context, issueID, columnID int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if len(f.moveErr) > 0 {
		err := f.moveErr[0]
		f.moveErr = f.moveErr[1:]
		return err
	}
	f.moves = append(f.moves, fakeMove{issueID, columnID})
	if is := f.issues[issueID]; is != nil {
		is.ColumnID = columnID
	}
	return nil
}

func (f *fakeItsaplan) Moves() []fakeMove {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeMove(nil), f.moves...)
}

func (f *fakeItsaplan) Results(id int64) []itsaplan.Result {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]itsaplan.Result(nil), f.get(id).results...)
}

func (f *fakeItsaplan) Comments() []fakeComment {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeComment(nil), f.comments...)
}

// fakeKagent models the v1.0.1 gateway: initial sends deduplicate on the
// message ID, a session belongs to the Agent that created it and runs one
// non-quiescent task at a time, and cancellation completes asynchronously.
type fakeKagent struct {
	mu         sync.Mutex
	tasks      map[string]*fakeTask
	order      []string
	contexts   map[string]kagent.AgentRef
	initial    map[string]string // message ID -> task ID
	sends      []fakeSend
	executions int
	calls      int
	// loseResponse accepts the next send but loses its response.
	loseResponse bool
	// dropSend loses the next send before kagent accepts it.
	dropSend bool
	// dropFor loses every send of these message IDs before acceptance;
	// loseFor accepts them but loses every response.
	dropFor map[string]bool
	loseFor map[string]bool
	// notAccepted rejects the next sends with KAGENT_SEND_NOT_ACCEPTED.
	notAccepted int
	// lateAccept takes the next send's dispatch reservation but loses the
	// response; the message persists only when acceptPending runs within
	// the reservation. Until then other sends to that session are not
	// accepted. lateAcceptAll does the same for every send that reserves.
	lateAccept    bool
	lateAcceptAll bool
	pending       *fakeSend
	pendingAt     time.Time
	now           func() time.Time
	// sendErr fails the next sends before anything is dispatched.
	sendErr []error
	// sessionErr fails the next GetSession calls.
	sessionErr []error
	sessions   map[string]*kagent.Session
	reverts    int
	// revertErr fails the next RevertSession calls.
	revertErr []error
	nctx      int
}

type fakeTask struct {
	task            kagent.Task
	agent           kagent.AgentRef
	cancelRequested bool
}

// settle completes a requested cancellation on the next read.
func (t *fakeTask) settle() {
	if t.cancelRequested && !kagent.Terminal(t.task.Status.State) {
		t.task.Status = kagent.TaskStatus{State: kagent.StateCanceled}
	}
}

type fakeSend struct {
	agent kagent.AgentRef
	msg   kagent.Message
}

// reservation is how long kagent holds a dispatch before rejecting its
// late first save.
const reservation = 2 * time.Minute

func newFakeKagent(now func() time.Time) *fakeKagent {
	return &fakeKagent{
		now:   now,
		tasks: map[string]*fakeTask{}, contexts: map[string]kagent.AgentRef{},
		initial: map[string]string{}, sessions: map[string]*kagent.Session{},
	}
}

func copyTask(t *kagent.Task) *kagent.Task {
	c := *t
	c.History = append([]*kagent.Message(nil), t.History...)
	return &c
}

func (k *fakeKagent) Send(_ context.Context, agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	k.sends = append(k.sends, fakeSend{agent, msg})
	if k.dropFor[msg.MessageID] {
		return nil, fmt.Errorf("%w: connection reset", kagent.ErrAmbiguous)
	}
	if k.loseFor[msg.MessageID] {
		if _, err := k.accept(agent, msg); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("%w: response lost", kagent.ErrAmbiguous)
	}
	if len(k.sendErr) > 0 {
		err := k.sendErr[0]
		k.sendErr = k.sendErr[1:]
		return nil, err
	}
	if k.pending != nil && k.now().Sub(k.pendingAt) >= reservation {
		k.pending = nil // the reservation expired; its late save is rejected
	}
	if k.pending != nil {
		// A reservation in flight blocks other dispatches to its session. A
		// first send with another message ID opens a different session.
		same := k.pending.msg.MessageID == msg.MessageID ||
			(msg.ContextID != "" && k.pending.msg.ContextID == msg.ContextID)
		if same {
			return nil, fmt.Errorf("%w: dispatch busy", kagent.ErrNotAccepted)
		}
	}
	if msg.ContextID == "" {
		if id, ok := k.initial[msg.MessageID]; ok {
			return copyTask(&k.tasks[id].task), nil
		}
	}
	if k.lateAccept || k.lateAcceptAll {
		k.lateAccept = false
		p := fakeSend{agent, msg}
		k.pending, k.pendingAt = &p, k.now()
		return nil, fmt.Errorf("%w: response lost", kagent.ErrAmbiguous)
	}
	return k.accept(agent, msg)
}

// acceptPending persists the in-flight message, as a slow runtime's first
// save would.
func (k *fakeKagent) acceptPending() {
	k.mu.Lock()
	defer k.mu.Unlock()
	p := k.pending
	k.pending = nil
	if p == nil || k.now().Sub(k.pendingAt) >= reservation {
		return // kagent rejects a first save after its reservation
	}
	if _, err := k.accept(p.agent, p.msg); err != nil {
		panic(err)
	}
}

// expirePending drops the in-flight message, as kagent rejects a first save
// after its reservation expires.
func (k *fakeKagent) expirePending() {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.pending = nil
}

func (k *fakeKagent) accept(agent kagent.AgentRef, msg kagent.Message) (*kagent.Task, error) {
	if k.dropSend {
		k.dropSend = false
		return nil, fmt.Errorf("%w: connection reset", kagent.ErrAmbiguous)
	}
	if k.notAccepted > 0 {
		k.notAccepted--
		return nil, fmt.Errorf("%w: busy", kagent.ErrNotAccepted)
	}
	var ctxID string
	if msg.ContextID == "" {
		if id, ok := k.initial[msg.MessageID]; ok {
			return copyTask(&k.tasks[id].task), nil
		}
		k.nctx++
		ctxID = fmt.Sprintf("ctx-%d", k.nctx)
		k.contexts[ctxID] = agent
		k.sessions[ctxID] = &kagent.Session{ID: ctxID, ContextID: ctxID, State: kagent.RuntimeReady}
	} else {
		owner, ok := k.contexts[msg.ContextID]
		if !ok || owner != agent {
			return nil, fmt.Errorf("%w: context", kagent.ErrTaskNotFound)
		}
		// A task-opening message is accepted once per session.
		for _, id := range k.order {
			t := k.tasks[id]
			if t.task.ContextID == msg.ContextID && t.task.History[0].MessageID == msg.MessageID {
				return copyTask(&t.task), nil
			}
		}
		for _, t := range k.tasks {
			if t.task.ContextID == msg.ContextID && !kagent.Quiescent(t.task.Status.State) {
				return nil, fmt.Errorf("%w: session has a running task", kagent.ErrNotAccepted)
			}
		}
		ctxID = msg.ContextID
	}
	id := fmt.Sprintf("task-%d", len(k.order)+1)
	m := msg
	m.Role = "ROLE_USER" // as kagent.A2A.Send sets it
	m.ContextID = ctxID
	m.TaskID = id
	k.tasks[id] = &fakeTask{agent: agent, task: kagent.Task{
		ID: id, ContextID: ctxID, Status: kagent.TaskStatus{State: kagent.StateWorking},
		History: []*kagent.Message{&m},
	}}
	k.order = append(k.order, id)
	if msg.ContextID == "" {
		k.initial[msg.MessageID] = id
	}
	k.executions++
	if k.loseResponse {
		k.loseResponse = false
		return nil, fmt.Errorf("%w: response lost", kagent.ErrAmbiguous)
	}
	return copyTask(&k.tasks[id].task), nil
}

func (k *fakeKagent) GetTask(_ context.Context, agent kagent.AgentRef, id string) (*kagent.Task, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	t, ok := k.tasks[id]
	if !ok || t.agent != agent {
		return nil, fmt.Errorf("%w: task", kagent.ErrTaskNotFound)
	}
	t.settle()
	return copyTask(&t.task), nil
}

func (k *fakeKagent) ListTasks(_ context.Context, agent kagent.AgentRef, contextID, state, _ string) ([]*kagent.Task, string, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	if contextID != "" {
		if owner, ok := k.contexts[contextID]; !ok || owner != agent {
			return nil, "", fmt.Errorf("%w: context", kagent.ErrTaskNotFound)
		}
	}
	var out []*kagent.Task
	for _, id := range k.order {
		t := k.tasks[id]
		t.settle()
		if t.agent == agent && (contextID == "" || t.task.ContextID == contextID) && (state == "" || t.task.Status.State == state) {
			c := copyTask(&t.task)
			c.History = nil
			out = append(out, c)
		}
	}
	return out, "", nil
}

func (k *fakeKagent) CancelTask(_ context.Context, agent kagent.AgentRef, id string) (*kagent.Task, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	t, ok := k.tasks[id]
	if !ok || t.agent != agent {
		return nil, fmt.Errorf("%w: task", kagent.ErrTaskNotFound)
	}
	if kagent.Terminal(t.task.Status.State) {
		return nil, errors.New("task not cancelable")
	}
	// Cancellation is asynchronous: the task still reports working now.
	t.cancelRequested = true
	return copyTask(&t.task), nil
}

func (k *fakeKagent) GetSession(_ context.Context, id string) (*kagent.Session, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	if len(k.sessionErr) > 0 {
		err := k.sessionErr[0]
		k.sessionErr = k.sessionErr[1:]
		return nil, err
	}
	s, ok := k.sessions[id]
	if !ok {
		return nil, kagent.ErrSessionNotFound
	}
	c := *s
	return &c, nil
}

func (k *fakeKagent) RevertSession(_ context.Context, id string) (*kagent.Session, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.calls++
	k.reverts++
	if len(k.revertErr) > 0 {
		err := k.revertErr[0]
		k.revertErr = k.revertErr[1:]
		return nil, err
	}
	s, ok := k.sessions[id]
	if !ok {
		return nil, kagent.ErrSessionNotFound
	}
	if s.Crashed() {
		// Reverting fails the interrupted turn instead of replaying it.
		for _, t := range k.tasks {
			if t.task.ContextID == id && !kagent.Quiescent(t.task.Status.State) {
				t.task.Status = kagent.TaskStatus{State: kagent.StateFailed}
			}
		}
	}
	s.State, s.Operation, s.FailureReason = kagent.RuntimeReady, 5, ""
	c := *s
	return &c, nil
}

// crash marks a session's runtime crashed; its running task keeps reporting
// working until a revert.
func (k *fakeKagent) crash(id string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	s := k.sessions[id]
	s.State, s.Operation, s.FailureReason = kagent.RuntimeFailed, kagent.OperationRevert, kagent.ReasonRuntimeCrashed
}

// expire deletes a session, as kagent's idle expiration does.
func (k *fakeKagent) expire(id string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	delete(k.sessions, id)
	delete(k.contexts, id)
}

// finish ends a task with an agent reply, as the runtime would.
func (k *fakeKagent) finish(id, state, text string, usage map[string]any) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t := k.tasks[id]
	reply := &kagent.Message{
		MessageID: id + "-reply", ContextID: t.task.ContextID, TaskID: id, Role: "ROLE_AGENT",
		Parts: []kagent.Part{{Text: text}},
	}
	if usage != nil {
		reply.Metadata = map[string]any{kagent.UsageMetadataKey: usage}
	}
	t.task.History = append(t.task.History, reply)
	t.task.Status = kagent.TaskStatus{State: state, Message: reply}
}

func (k *fakeKagent) say(id, text string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	t := k.tasks[id]
	t.task.History = append(t.task.History, &kagent.Message{
		MessageID: fmt.Sprintf("%s-m%d", id, len(t.task.History)), Role: "ROLE_AGENT",
		Parts: []kagent.Part{{Text: text}},
	})
}

func (k *fakeKagent) Sends() []fakeSend {
	k.mu.Lock()
	defer k.mu.Unlock()
	return append([]fakeSend(nil), k.sends...)
}

func (k *fakeKagent) Executions() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.executions
}

func (k *fakeKagent) Calls() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.calls
}

func (k *fakeKagent) lastTask() string {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.order[len(k.order)-1]
}

func (k *fakeKagent) state(id string) string {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.tasks[id].task.Status.State
}
