package guestmem

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
)

type State string

const (
	Pending     State = "pending"
	OK          State = "ok"
	Stale       State = "stale"
	Unsupported State = "unsupported"
	Error       State = "error"
)

// Target is host-minted attribution. VM is the stable bundle ID; Activation is
// the handle's per-claim ID, which changes on every restore of that bundle.
type (
	Target struct{ VM, Workload, Activation, UDS string }
	Event  struct {
		Target
		Kind  string
		Delta uint64
	}
)

type Fetcher interface {
	Fetch(context.Context, string, string) (guestagent.MemoryStatus, error)
}
type Options struct {
	Enabled  bool
	Interval time.Duration
	Client   Fetcher
	Now      func() time.Time
	Event    func(Event)
}
type observation struct {
	target   Target
	state    State
	lastGood time.Time
	lastOOM  *uint64
	sample   *guestagent.MemoryStatus
	cancel   context.CancelFunc
	done     chan struct{}
}
type Poller struct {
	mu       sync.Mutex
	opts     Options
	entries  map[string]*observation
	guestOOM uint64
}
type Aggregates struct {
	Counts   map[string]uint64
	GuestOOM uint64
}

func New(opts Options) *Poller {
	if opts.Interval < time.Second {
		opts.Interval = 10 * time.Second
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Client == nil {
		opts.Client = Client{}
	}
	return &Poller{opts: opts, entries: make(map[string]*observation)}
}

// Track replaces the old activation, cancelling its outstanding request. A
// duplicate Track of the same handle preserves its baseline and support probe.
func (p *Poller) Track(t Target) {
	if !p.opts.Enabled {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if old := p.entries[t.VM]; old != nil {
		if old.target.Activation == t.Activation {
			return
		}
		if old.cancel != nil {
			old.cancel()
		}
	}
	p.entries[t.VM] = &observation{target: t, state: Pending}
}

// Forget cannot discard a newer activation while an older teardown finishes.
func (p *Poller) Forget(vm, activation string) {
	p.mu.Lock()
	var done chan struct{}
	if old := p.entries[vm]; old != nil && old.target.Activation == activation {
		if old.cancel != nil {
			old.cancel()
		}
		delete(p.entries, vm)
		done = old.done
	}
	p.mu.Unlock()
	// A bank waits for its cancelled request before pausing the guest, so no
	// command crosses the snapshot barrier. The real client has a hard budget.
	if done != nil {
		<-done
	}
}

// Run is daemon-scoped. Unsupported agents are probed only once per activation.
// Polling is serial and each connection has a one-second budget, bounding work.
func (p *Poller) Run(ctx context.Context) {
	if !p.opts.Enabled {
		return
	}
	ticker := time.NewTicker(p.opts.Interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.Poll(ctx)
		}
	}
}

// Poll is also the deterministic fake-clock seam. Reports are committed only
// if their exact observation still belongs to the currently tracked activation.
func (p *Poller) Poll(ctx context.Context) {
	if !p.opts.Enabled {
		return
	}
	p.mu.Lock()
	entries := make([]*observation, 0, len(p.entries))
	for _, e := range p.entries {
		entries = append(entries, e)
	}
	p.mu.Unlock()
	for _, e := range entries {
		if ctx.Err() != nil {
			return
		}
		p.mu.Lock()
		if p.entries[e.target.VM] != e || e.state == Unsupported || e.cancel != nil {
			p.mu.Unlock()
			continue
		}
		p.refresh(e)
		requestCtx, cancel := context.WithCancel(ctx)
		e.cancel = cancel
		e.done = make(chan struct{})
		p.mu.Unlock()
		var nonceBytes [16]byte
		_, err := rand.Read(nonceBytes[:])
		var sample guestagent.MemoryStatus
		nonce := hex.EncodeToString(nonceBytes[:])
		if err == nil {
			sample, err = p.opts.Client.Fetch(requestCtx, e.target.UDS, nonce)
		}
		if err == nil {
			err = sample.Validate()
			if err == nil && sample.Nonce != nonce {
				err = errors.New("memory response nonce mismatch")
			}
		}
		cancel()
		p.mu.Lock()
		close(e.done)
		if p.entries[e.target.VM] != e {
			p.mu.Unlock()
			continue
		}
		e.cancel = nil
		e.done = nil
		if err != nil {
			e.sample = nil
			if errors.Is(err, ErrUnsupported) {
				p.transition(e, Unsupported)
			} else if !e.lastGood.IsZero() && p.opts.Now().Sub(e.lastGood) >= 3*p.opts.Interval {
				p.transition(e, Stale)
			} else {
				p.transition(e, Error)
			}
		} else if sample.OOMKill != nil && e.lastOOM != nil && *sample.OOMKill < *e.lastOOM {
			// A backwards counter is invalid evidence, not a restore or an OOM.
			e.sample = nil
			p.transition(e, Error)
			p.refresh(e)
		} else {
			if sample.OOMKill != nil && e.lastOOM != nil && *sample.OOMKill > *e.lastOOM {
				delta := *sample.OOMKill - *e.lastOOM
				p.guestOOM += delta
				p.emit(Event{Target: e.target, Kind: "guest_oom", Delta: delta})
			}
			// Retain the last supported counter across temporary unsupported reports.
			if sample.OOMKill != nil {
				v := *sample.OOMKill
				e.lastOOM = &v
			}
			e.lastGood, e.sample = p.opts.Now(), &sample
			p.transition(e, OK)
		}
		p.mu.Unlock()
	}
}

func (p *Poller) emit(e Event) {
	if p.opts.Event != nil {
		p.opts.Event(e)
	}
}

func (p *Poller) transition(e *observation, state State) {
	if e.state == state {
		return
	}
	e.state = state
	if state == Stale || state == Unsupported {
		p.emit(Event{Target: e.target, Kind: string(state)})
	}
}

func (p *Poller) refresh(e *observation) {
	if e.state != Unsupported && !e.lastGood.IsZero() && p.opts.Now().Sub(e.lastGood) >= 3*p.opts.Interval {
		e.sample = nil
		p.transition(e, Stale)
	}
}

// Snapshot has fixed cardinality. There are no VM, activation or guest labels.
// It ages evidence even if a busy polling sweep has not reached this VM yet.
func (p *Poller) Snapshot() Aggregates {
	p.mu.Lock()
	defer p.mu.Unlock()
	a := Aggregates{Counts: map[string]uint64{"pending": 0, "ok": 0, "stale": 0, "unsupported": 0, "error": 0}, GuestOOM: p.guestOOM}
	for _, e := range p.entries {
		p.refresh(e)
		a.Counts[string(e.state)]++
	}
	return a
}
