package review

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/reviewer/metrics"
)

// fakeCleaner serves pages of sessions and records names and deletions.
type fakeCleaner struct {
	mu        sync.Mutex
	pages     [][]*kagent.Session
	listErr   map[int]error
	endless   bool
	lists     int
	deleted   []string
	deleteErr map[string]error
	names     map[string]string
}

func (c *fakeCleaner) ListSessions(_ context.Context, agent kagent.AgentRef, token string) ([]*kagent.Session, string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lists++
	if agent != testAgent {
		return nil, "", fmt.Errorf("unexpected agent %v", agent)
	}
	i := 0
	if token != "" {
		i, _ = strconv.Atoi(token)
	}
	if err := c.listErr[i]; err != nil {
		return nil, "", err
	}
	if c.endless {
		return nil, strconv.Itoa(i + 1), nil
	}
	if i >= len(c.pages) {
		return nil, "", nil
	}
	next := ""
	if i+1 < len(c.pages) {
		next = strconv.Itoa(i + 1)
	}
	return c.pages[i], next, nil
}

func (c *fakeCleaner) DeleteSession(_ context.Context, id string) (*kagent.Session, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.deleted = append(c.deleted, id)
	if err := c.deleteErr[id]; err != nil {
		return nil, err
	}
	return &kagent.Session{ID: id, State: kagent.RuntimeDeleting}, nil
}

func (c *fakeCleaner) NameSession(_ context.Context, id, name string) (*kagent.Session, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.names == nil {
		c.names = map[string]string{}
	}
	c.names[id] = name
	return &kagent.Session{ID: id, Name: name}, nil
}

func (c *fakeCleaner) deletions() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Sorted(slices.Values(c.deleted))
}

// sweepTasks answers ListTasks per session; only the sweep calls it.
type sweepTasks struct {
	pages map[string]*kagent.TaskPage
	errs  map[string]error
}

func (s *sweepTasks) Send(context.Context, kagent.AgentRef, kagent.Message) (*kagent.Task, error) {
	return nil, errors.New("unused")
}

func (s *sweepTasks) GetTask(context.Context, kagent.AgentRef, string) (*kagent.Task, error) {
	return nil, errors.New("unused")
}

func (s *sweepTasks) CancelTask(context.Context, kagent.AgentRef, string) (*kagent.Task, error) {
	return nil, errors.New("unused")
}

func (s *sweepTasks) ListTasks(_ context.Context, _ kagent.AgentRef, contextID, _, _ string) (*kagent.TaskPage, error) {
	if err := s.errs[contextID]; err != nil {
		return nil, err
	}
	if p := s.pages[contextID]; p != nil {
		return p, nil
	}
	return &kagent.TaskPage{Tasks: []*kagent.Task{}, PageSize: 100}, nil
}

func tasksAt(states ...any) *kagent.TaskPage {
	p := &kagent.TaskPage{PageSize: 100}
	for i := 0; i+1 < len(states); i += 2 {
		p.Tasks = append(p.Tasks, &kagent.Task{
			ID: fmt.Sprintf("t%d", i), Status: kagent.TaskStatus{State: states[i].(string), Timestamp: states[i+1].(time.Time).Format(time.RFC3339Nano)},
		})
	}
	p.TotalSize = len(p.Tasks)
	return p
}

var sweepNow = time.Date(2026, 11, 16, 18, 0, 0, 0, time.UTC)

func sweepEngine(c *fakeCleaner, tasks Tasks) (*Engine, *metrics.Metrics) {
	m := metrics.New(nil)
	e := New(Config{
		Bindings: []Binding{{Name: "homelab", Owner: "example-org", Repo: "homelab", BaseBranch: "main", Agent: testAgent}},
		Cleaner:  c, Now: func() time.Time { return sweepNow },
	}, nil, tasks, m, nil)
	return e, m
}

func sess(id, name string, age time.Duration) *kagent.Session {
	return &kagent.Session{
		ID: id, ContextID: id, Name: name, State: kagent.RuntimeReady, CreatedAt: sweepNow.Add(-age),
		AgentNamespace: testAgent.Namespace, AgentName: testAgent.Name,
	}
}

// TestSweepDeletesSessionsNoReviewResumes keeps each open PR's newest
// session and deletes the rest: closed PRs' sessions, superseded ones and
// long-idle unnamed ones. It leaves new, busy, foreign and gone sessions.
func TestSweepDeletesSessionsNoReviewResumes(t *testing.T) {
	name := func(n int) string { return SessionName(testRepo, n) }
	gone := sess("gone", name(8), 3*time.Hour)
	gone.State = kagent.RuntimeDeleted
	otherAgent := sess("other-agent", name(8), 3*time.Hour)
	otherAgent.AgentName = "coder"
	// A delete kagent admitted but did not finish; newer than pr7-new, yet
	// not the session PR 7 resumes.
	stuck := sess("pr7-stuck", name(7), 30*time.Minute)
	stuck.State = kagent.RuntimeDeleting
	stuckYoung := sess("pr9-stuck-young", name(9), 5*time.Minute)
	stuckYoung.State = kagent.RuntimeDeleting
	c := &fakeCleaner{
		pages: [][]*kagent.Session{
			{
				sess("pr7-old", name(7), 3*time.Hour),
				sess("pr7-new", name(7), time.Hour),
				sess("pr8", name(8), 2*time.Hour),
				sess("pr9-young", name(9), 5*time.Minute),
			},
			{
				sess("unnamed-idle", "", 20*time.Hour),
				sess("unnamed-recent", "", 20*time.Hour),
				sess("pr10-busy", name(10), 2*time.Hour),
				sess("other-repo", "factory-review/example-org/other/8", 2*time.Hour),
				gone, otherAgent, stuck, stuckYoung,
				sess("pr11-refused", name(11), 2*time.Hour),
				sess("pr12-unreadable", name(12), 2*time.Hour),
				sess("pr13-only", name(13), 2*time.Hour),
			},
		},
		deleteErr: map[string]error{"pr11-refused": errors.New("rpc error: code = FailedPrecondition desc = active turns")},
	}
	tasks := &sweepTasks{
		pages: map[string]*kagent.TaskPage{
			"unnamed-idle":   tasksAt(kagent.StateCompleted, sweepNow.Add(-13*time.Hour)),
			"unnamed-recent": tasksAt(kagent.StateCompleted, sweepNow.Add(-15*time.Hour), kagent.StateCompleted, sweepNow.Add(-time.Hour)),
			"pr10-busy":      tasksAt(kagent.StateCompleted, sweepNow.Add(-time.Hour), kagent.StateWorking, sweepNow.Add(-time.Minute)),
		},
		errs: map[string]error{"pr12-unreadable": errors.New("kagent: rpc -32603: internal error")},
	}
	e, m := sweepEngine(c, tasks)
	open := map[int]bool{7: true, 13: true}
	e.sweep(context.Background(), &e.cfg.Bindings[0], open, e.log)

	// pr11-refused is attempted and refused; every other kept session is
	// never sent a delete.
	want := []string{"pr11-refused", "pr7-old", "pr7-stuck", "pr8", "unnamed-idle"}
	if got := c.deletions(); !slices.Equal(got, want) {
		t.Fatalf("deleted %v, want %v", got, want)
	}
	for reason, n := range map[string]float64{cleanupClosed: 1, cleanupSuperseded: 1, cleanupIdle: 1, cleanupRetry: 1} {
		if got := testutil.ToFloat64(m.SessionsDeleted.WithLabelValues("homelab", reason)); got != n {
			t.Errorf("deleted %s = %v, want %v", reason, got, n)
		}
	}
	for call, n := range map[string]float64{"delete_session": 1, "list_tasks": 1} {
		if got := testutil.ToFloat64(m.KagentErrors.WithLabelValues("homelab", call)); got != n {
			t.Errorf("kagent %s errors = %v, want %v", call, got, n)
		}
	}
}

// TestSweepDeletesNothingFromAPartialListing deletes no session when the
// listing failed or ran past maxSessionPages: an unlisted session may be the
// newest of its PR.
func TestSweepDeletesNothingFromAPartialListing(t *testing.T) {
	pages := [][]*kagent.Session{{sess("pr7-old", SessionName(testRepo, 7), 3*time.Hour)}, {sess("pr8", SessionName(testRepo, 8), 2*time.Hour)}}
	for name, c := range map[string]*fakeCleaner{
		"failed page": {pages: pages, listErr: map[int]error{1: errors.New("unavailable")}},
		"endless":     {endless: true},
	} {
		t.Run(name, func(t *testing.T) {
			e, _ := sweepEngine(c, &sweepTasks{})
			e.sweep(context.Background(), &e.cfg.Bindings[0], map[int]bool{}, e.log)
			if got := c.deletions(); len(got) != 0 {
				t.Fatalf("deleted %v from a partial listing", got)
			}
			if name == "endless" && c.lists != maxSessionPages {
				t.Fatalf("listed %d pages, want %d", c.lists, maxSessionPages)
			}
		})
	}
}

// TestSweepSessionsNeedsACompleteListingAndSpacing sweeps only after a
// complete open-PR listing, at most every cleanupEvery per binding.
func TestSweepSessionsNeedsACompleteListingAndSpacing(t *testing.T) {
	c := &fakeCleaner{}
	now := sweepNow
	e := New(Config{
		Bindings: []Binding{{Name: "homelab", Owner: "example-org", Repo: "homelab", BaseBranch: "main", Agent: testAgent}},
		Cleaner:  c, Now: func() time.Time { return now },
	}, nil, &sweepTasks{}, nil, nil)
	b := &e.cfg.Bindings[0]
	sweeps := func() int {
		e.Wait()
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.lists
	}
	prs := []github.PullRequest{{Number: 7}}
	e.sweepSessions(context.Background(), b, prs, false, e.log)
	if n := sweeps(); n != 0 {
		t.Fatalf("swept %d times after an incomplete listing", n)
	}
	e.sweepSessions(context.Background(), b, prs, true, e.log)
	if n := sweeps(); n != 1 {
		t.Fatalf("swept %d times, want 1", n)
	}
	now = now.Add(cleanupEvery - time.Second)
	e.sweepSessions(context.Background(), b, prs, true, e.log)
	if n := sweeps(); n != 1 {
		t.Fatalf("swept again after %s", cleanupEvery-time.Second)
	}
	now = now.Add(time.Second)
	e.sweepSessions(context.Background(), b, prs, true, e.log)
	if n := sweeps(); n != 2 {
		t.Fatalf("swept %d times after %s, want 2", n, cleanupEvery)
	}
	off := New(Config{Bindings: e.cfg.Bindings, Now: func() time.Time { return now }}, nil, &sweepTasks{}, nil, nil)
	off.sweepSessions(context.Background(), &off.cfg.Bindings[0], prs, true, off.log)
	off.Wait()
}

// TestReviewNamesItsSessionAfterThePR names the session a review task runs
// in, so a later sweep knows its PR.
func TestReviewNamesItsSessionAfterThePR(t *testing.T) {
	h := newHarness(t)
	c := &fakeCleaner{}
	h.cfg.Cleaner = c
	addPR7(h)
	e, ctx, _ := h.start()
	finishHead(t, h, e, ctx, shaA, passing, nil)
	ctxID := "ctx-" + h.taskOn(7, shaA, 1)
	c.mu.Lock()
	got := c.names[ctxID]
	c.mu.Unlock()
	if got != "factory-review/example-org/homelab/7" {
		t.Fatalf("session %s named %q", ctxID, got)
	}
}

func TestSessionPR(t *testing.T) {
	for name, want := range map[string]int{
		"factory-review/example-org/homelab/7":    7,
		"factory-review/example-org/homelab/0":    0,
		"factory-review/example-org/homelab/07":   0,
		"factory-review/example-org/homelab/7/x":  0,
		"factory-review/example-org/homelab2/7":   0,
		"factory-review/example-org/homelab/":     0,
		"factory-review/example-org/homelab/+7":   0,
		"factory-review/example-org/homelab/-7":   0,
		"":                                        0,
		"factory-review/example-org/homelab/8110": 8110,
	} {
		n, ok := sessionPR(name, testRepo)
		if (want > 0) != ok || (ok && n != want) {
			t.Errorf("sessionPR(%q) = %d, %v; want %d", name, n, ok, want)
		}
	}
}
