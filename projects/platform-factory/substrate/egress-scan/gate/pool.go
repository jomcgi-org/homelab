package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// A child is a fork actor of the engine template. It listens, holds one engine
// process that has not read a byte, and serves exactly one request. Then it
// exits and the control plane deletes it.
type child struct {
	name string
	// started is when the gate began the fork. The child's maximum lifetime
	// runs from the fork, so this is a little early: the age is never low.
	started time.Time
}

var (
	// errPoolEmpty: no waiting child, and path A cannot run in the time left.
	errPoolEmpty = errors.New("no waiting child")
	// errNoWorker: a create failed a moment ago, so path A is refused at once.
	errNoWorker = errors.New("recent create failed")
)

// pool keeps waiting children and hands each one out once. The ate API has no
// claim operation, so a claim is a local pop under one mutex: two requests
// can never get the same child.
type pool struct {
	cfg config
	api actorAPI
	m   *metrics
	log *limitedLog
	now func() time.Time

	epoch int64
	seq   atomic.Uint64
	// baseCtx outlives a request. Background work and cleanup use it.
	baseCtx context.Context
	bg      sync.WaitGroup
	fbSem   chan struct{}

	mu        sync.Mutex
	waiting   []*child // oldest first
	creating  map[string]struct{}
	served    map[string]time.Time // handed out, until the sweep finds them gone
	orphans   map[string]bool      // names seen untracked by the last sweep
	started   bool                 // the startup cleanup finished
	lastFail  time.Time
	backoff   time.Duration
	retryAt   time.Time
	lastAPIOK time.Time
	wake      chan struct{}
}

func newPool(ctx context.Context, cfg config, api actorAPI, m *metrics, log *limitedLog) *pool {
	p := &pool{
		cfg: cfg, api: api, m: m, log: log, now: time.Now,
		baseCtx:  ctx,
		fbSem:    make(chan struct{}, cfg.MaxFallback),
		creating: map[string]struct{}{},
		served:   map[string]time.Time{},
		orphans:  map[string]bool{},
		wake:     make(chan struct{}, 1),
	}
	p.epoch = p.now().Unix()
	return p
}

func (p *pool) newName() string { return fmt.Sprintf("%s%d-%d", p.cfg.prefix(), p.epoch, p.seq.Add(1)) }

func (p *pool) signal() {
	select {
	case p.wake <- struct{}{}:
	default:
	}
}

// observe records that the ate API answered. A missing actor is an answer.
func (p *pool) observe(err error) {
	if err == nil || errors.Is(err, errNotFound) {
		p.mu.Lock()
		p.lastAPIOK = p.now()
		p.mu.Unlock()
	}
}

func (p *pool) gaugesLocked() {
	p.m.children.WithLabelValues("waiting").Set(float64(len(p.waiting)))
	p.m.children.WithLabelValues("creating").Set(float64(len(p.creating)))
}

// ready reports whether the gate may take traffic: the startup cleanup is
// done, one child waits and the API answered recently.
func (p *pool) ready() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.started && len(p.waiting) >= 1 && p.now().Sub(p.lastAPIOK) < 2*p.cfg.SweepInterval
}

// run does the startup cleanup, then keeps the pool full until ctx ends.
func (p *pool) run(ctx context.Context) {
	for wait := time.Second; ; wait = min(wait*2, 10*time.Second) {
		err := p.cleanup(ctx)
		if err == nil {
			break
		}
		p.log.warn("cleanup", "startup cleanup failed, retrying", "error", err.Error())
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
	p.mu.Lock()
	p.started = true
	p.mu.Unlock()
	reconcile, sweep := time.NewTicker(500*time.Millisecond), time.NewTicker(p.cfg.SweepInterval)
	defer reconcile.Stop()
	defer sweep.Stop()
	for {
		p.reconcile()
		select {
		case <-ctx.Done():
			return
		case <-p.wake:
		case <-reconcile.C:
		case <-sweep.C:
			p.sweep(ctx)
		}
	}
}

// cleanup deletes every actor that carries this pod's name prefix: the
// children of an earlier run, which keep their booking until they expire.
func (p *pool) cleanup(ctx context.Context) error {
	actors, err := p.listOwn(ctx)
	if err != nil {
		return err
	}
	var failed error
	for _, a := range actors {
		if err := p.deleteOne(ctx, a.Name); err != nil {
			failed = err
		}
	}
	return failed
}

func (p *pool) listOwn(ctx context.Context) ([]actor, error) {
	cctx, cancel := context.WithTimeout(ctx, p.cfg.APICallTimeout)
	defer cancel()
	actors, err := p.api.List(cctx, p.cfg.prefix())
	p.observe(err)
	if err != nil {
		p.m.replenish.WithLabelValues("list").Inc()
	}
	return actors, err
}

// deleteOne deletes a child. A child that is already gone is not an error.
func (p *pool) deleteOne(ctx context.Context, name string) error {
	cctx, cancel := context.WithTimeout(ctx, p.cfg.APICallTimeout)
	defer cancel()
	err := p.api.Delete(cctx, name)
	p.observe(err)
	if err != nil && !errors.Is(err, errNotFound) {
		p.m.replenish.WithLabelValues("delete").Inc()
		return err
	}
	return nil
}

// deleteAsync deletes a child off the request path, with a context that no
// request can cancel.
func (p *pool) deleteAsync(name string) {
	p.bg.Add(1)
	go func() {
		defer p.bg.Done()
		if err := p.deleteOne(context.WithoutCancel(p.baseCtx), name); err != nil {
			p.log.warn("delete", "child delete failed", "child", name, "error", err.Error())
		}
	}()
}

// reconcile keeps TargetWaiting fresh children, replaces the old ones and
// starts the creators.
func (p *pool) reconcile() {
	now := p.now()
	var drop []*child
	p.mu.Lock()
	if !p.started {
		p.mu.Unlock()
		return
	}
	fresh := 0
	for _, c := range p.waiting {
		if now.Sub(c.started) < p.cfg.RecycleAfter {
			fresh++
		}
	}
	keep := p.waiting[:0]
	for _, c := range p.waiting {
		age := now.Sub(c.started)
		// An old child is replaced when fresh ones wait, or when it comes close
		// to the maximum lifetime. A delete is no crash; an expiry is reported
		// as one.
		if age >= p.cfg.HardAfter || (age >= p.cfg.RecycleAfter && fresh >= p.cfg.TargetWaiting) {
			drop = append(drop, c)
			continue
		}
		keep = append(keep, c)
	}
	p.waiting = keep
	need := min(p.cfg.TargetWaiting-fresh-len(p.creating), p.cfg.CreateConcurrency-len(p.creating))
	if now.Before(p.retryAt) {
		need = 0
	}
	var names []string
	for i := 0; i < need; i++ {
		name := p.newName()
		p.creating[name] = struct{}{}
		names = append(names, name)
	}
	p.gaugesLocked()
	p.mu.Unlock()

	for _, c := range drop {
		p.m.recycled.Inc()
		p.deleteAsync(c.name)
	}
	for _, name := range names {
		p.bg.Add(1)
		go p.replenish(name)
	}
}

// replenish creates one waiting child.
func (p *pool) replenish(name string) {
	defer p.bg.Done()
	ctx, cancel := context.WithTimeout(p.baseCtx, p.cfg.APICallTimeout*2)
	defer cancel()
	c, err := p.createOne(ctx, name)
	p.mu.Lock()
	delete(p.creating, name)
	if err != nil {
		p.noteFailureLocked()
	} else {
		p.backoff, p.lastFail = 0, time.Time{}
		p.waiting = append(p.waiting, c)
		sort.SliceStable(p.waiting, func(i, j int) bool { return p.waiting[i].started.Before(p.waiting[j].started) })
	}
	p.gaugesLocked()
	p.mu.Unlock()
	if err != nil {
		p.log.warn("replenish", "creating a waiting child failed", "child", name, "error", err.Error())
	}
	p.signal()
}

// noteFailureLocked starts the back-off after a failed create.
func (p *pool) noteFailureLocked() {
	p.lastFail = p.now()
	if p.now().Before(p.retryAt) {
		// Creates that failed together count as one failure.
		return
	}
	if p.backoff == 0 {
		p.backoff = 200 * time.Millisecond
	} else {
		p.backoff = min(p.backoff*2, 5*time.Second)
	}
	p.retryAt = p.now().Add(p.backoff)
}

// createOne creates a child and forks it. It returns when the child is
// RUNNING, so it waits for a request. A failed child is deleted.
func (p *pool) createOne(ctx context.Context, name string) (*child, error) {
	began := p.now()
	_, err := p.api.Create(ctx, name)
	p.m.createSeconds.Observe(time.Since(began).Seconds())
	p.observe(err)
	if err != nil {
		p.m.replenish.WithLabelValues("create").Inc()
		return nil, fmt.Errorf("create: %w", err)
	}
	forkStart := p.now()
	got, err := p.api.Resume(ctx, name)
	p.m.resumeSeconds.Observe(time.Since(forkStart).Seconds())
	p.observe(err)
	if err == nil && !got.Running {
		got, err = p.waitRunning(ctx, name)
	}
	if err != nil {
		p.m.replenish.WithLabelValues("resume").Inc()
		p.deleteAsync(name)
		return nil, fmt.Errorf("resume: %w", err)
	}
	return &child{name: name, started: forkStart}, nil
}

// waitRunning polls a child that Resume did not return as RUNNING.
func (p *pool) waitRunning(ctx context.Context, name string) (actor, error) {
	for {
		got, err := p.api.Get(ctx, name)
		p.observe(err)
		if err != nil || got.Running {
			return got, err
		}
		select {
		case <-ctx.Done():
			return got, fmt.Errorf("%w: child did not start", errUnavailable)
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// claim takes the newest waiting child. The newest child has the most
// lifetime left, and the old ones stay for the recycler. A child past
// HardAfter is never handed out.
func (p *pool) claim() (*child, bool) {
	began := time.Now()
	defer func() { p.m.claimSeconds.Observe(time.Since(began).Seconds()) }()
	now := p.now()
	var expired []*child
	var got *child
	p.mu.Lock()
	for len(p.waiting) > 0 {
		c := p.waiting[len(p.waiting)-1]
		p.waiting = p.waiting[:len(p.waiting)-1]
		if now.Sub(c.started) >= p.cfg.HardAfter {
			expired = append(expired, c)
			continue
		}
		p.served[c.name] = now
		got = c
		break
	}
	p.gaugesLocked()
	p.mu.Unlock()
	for _, c := range expired {
		p.m.recycled.Inc()
		p.deleteAsync(c.name)
	}
	p.signal()
	return got, got != nil
}

// release puts back a child whose body never left the gate.
func (p *pool) release(c *child) {
	p.mu.Lock()
	delete(p.served, c.name)
	p.waiting = append(p.waiting, c)
	sort.SliceStable(p.waiting, func(i, j int) bool { return p.waiting[i].started.Before(p.waiting[j].started) })
	p.gaugesLocked()
	p.mu.Unlock()
}

// retire deletes a child that failed. A delete of a gone child is harmless.
func (p *pool) retire(c *child) {
	p.m.childErrs.Inc()
	p.deleteAsync(c.name)
}

// fallbackChild is path A: create and fork a child now, because the pool is
// empty. It stays inside the request deadline and refuses at once when the
// last create failed a moment ago (no worker).
func (p *pool) fallbackChild(ctx context.Context) (*child, error) {
	p.mu.Lock()
	failedRecently := !p.lastFail.IsZero() && p.now().Sub(p.lastFail) < p.cfg.FailFast
	p.mu.Unlock()
	if failedRecently {
		p.m.fallbacks.WithLabelValues("refused").Inc()
		return nil, errNoWorker
	}
	if dl, ok := ctx.Deadline(); ok && time.Until(dl) < p.cfg.FallbackMinRemaining {
		p.m.fallbacks.WithLabelValues("skipped").Inc()
		return nil, errPoolEmpty
	}
	select {
	case p.fbSem <- struct{}{}:
		defer func() { <-p.fbSem }()
	default:
		p.m.fallbacks.WithLabelValues("skipped").Inc()
		return nil, errPoolEmpty
	}
	// The name is tracked as served from the start, so the sweep leaves a child
	// that is still being forked alone.
	name := p.newName()
	p.mu.Lock()
	p.served[name] = p.now()
	p.mu.Unlock()
	c, err := p.createOne(ctx, name)
	if err != nil && ctx.Err() == nil {
		// A late answer to a caller that gave up says nothing about the worker.
		p.mu.Lock()
		p.noteFailureLocked()
		p.mu.Unlock()
	}
	p.signal()
	if err != nil {
		p.m.fallbacks.WithLabelValues("failed").Inc()
		return nil, err
	}
	p.m.fallbacks.WithLabelValues("ok").Inc()
	return c, nil
}

// sweep deletes children that the gate no longer tracks: a served child that
// the control plane did not delete, or a child whose delete failed. It also
// proves that the API answers. A child is deleted only when two sweeps in a
// row find it untracked, and not before SweepGrace after its handout.
func (p *pool) sweep(ctx context.Context) {
	actors, err := p.listOwn(ctx)
	if err != nil {
		p.log.warn("sweep", "listing children failed", "error", err.Error())
		return
	}
	now := p.now()
	var kill []string
	p.mu.Lock()
	tracked := map[string]bool{}
	for _, c := range p.waiting {
		tracked[c.name] = true
	}
	for n := range p.creating {
		tracked[n] = true
	}
	present := map[string]bool{}
	spent := 0
	next := map[string]bool{}
	for _, a := range actors {
		present[a.Name] = true
		if tracked[a.Name] {
			continue
		}
		spent++
		if at, ok := p.served[a.Name]; ok && now.Sub(at) < p.cfg.SweepGrace {
			continue
		}
		if p.orphans[a.Name] {
			kill = append(kill, a.Name)
			continue
		}
		next[a.Name] = true
	}
	p.orphans = next
	for n := range p.served {
		if !present[n] {
			delete(p.served, n)
		}
	}
	p.m.spent.Set(float64(spent))
	p.mu.Unlock()
	for _, n := range kill {
		p.deleteAsync(n)
	}
}

// shutdown waits for background work and deletes every child of this pod.
func (p *pool) shutdown(ctx context.Context) {
	p.bg.Wait()
	if err := p.cleanup(ctx); err != nil {
		p.log.warn("shutdown", "deleting children failed", "error", err.Error())
	}
}

// limitedLog writes one line per key in each interval, with a count of the
// lines it dropped. An actor controls how many requests the gate sees, so one
// line per failure would let it flood the log. No line holds a request body.
type limitedLog struct {
	log   *slog.Logger
	every time.Duration
	now   func() time.Time

	mu         sync.Mutex
	last       map[string]time.Time
	suppressed map[string]int
}

func newLimitedLog(l *slog.Logger) *limitedLog {
	return &limitedLog{log: l, every: 10 * time.Second, now: time.Now, last: map[string]time.Time{}, suppressed: map[string]int{}}
}

func (l *limitedLog) warn(key, msg string, args ...any) {
	l.mu.Lock()
	now := l.now()
	if t, ok := l.last[key]; ok && now.Sub(t) < l.every {
		l.suppressed[key]++
		l.mu.Unlock()
		return
	}
	n := l.suppressed[key]
	delete(l.suppressed, key)
	l.last[key] = now
	l.mu.Unlock()
	if n > 0 {
		args = append(args, "suppressed", n)
	}
	l.log.Warn(msg, args...)
}

func (l *limitedLog) info(msg string, args ...any) { l.log.Info(msg, args...) }
