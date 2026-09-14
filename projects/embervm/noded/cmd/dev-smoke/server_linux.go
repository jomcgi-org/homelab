package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/cmd/dev-smoke/synthetic"
	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"github.com/jomcgi/homelab/projects/embervm/noded/vsockhttp"
)

const scanTimeout = 20 * time.Second

type scanReply struct {
	synthetic.Result
	VMID      string  `json:"vm_id"`
	RestoreMS float64 `json:"restore_ms"`
	GuestMS   float64 `json:"guest_round_trip_ms"`
	CleanupMS float64 `json:"cleanup_ms"`
	ServerMS  float64 `json:"server_ms"`
}

type scanOutcome struct {
	reply scanReply
	err   error
}

type scanJob struct {
	ctx     context.Context
	request synthetic.Request
	start   time.Time
	done    chan scanOutcome
}

type scanSlot struct {
	state string
	jobs  chan scanJob
}

type poolStatus struct {
	Session   string `json:"session"`
	Capacity  int    `json:"capacity"`
	Ready     int    `json:"ready"`
	Active    int    `json:"active"`
	Priming   int    `json:"priming"`
	Cleaning  int    `json:"cleaning"`
	Peak      int    `json:"peak_active"`
	Accepted  int    `json:"accepted"`
	Completed int    `json:"completed"`
	Rejected  int    `json:"rejected"`
	Failed    int    `json:"failed"`
	Canceled  int    `json:"canceled"`
	Draining  bool   `json:"draining"`
}

type scanPool struct {
	mu     sync.Mutex
	slots  []*scanSlot
	totals poolStatus
}

func (p *scanPool) statusLocked() poolStatus {
	s := p.totals
	s.Capacity = len(p.slots)
	for _, slot := range p.slots {
		switch slot.state {
		case "ready":
			s.Ready++
		case "busy":
			s.Active++
		case "priming":
			s.Priming++
		case "cleaning":
			s.Cleaning++
		}
	}
	return s
}

func (p *scanPool) setState(slot *scanSlot, state string) {
	p.mu.Lock()
	slot.state = state
	p.mu.Unlock()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (p *scanPool) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]bool{"live": true})
	})
	for _, path := range []string{"GET /status", "GET /readyz"} {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			p.mu.Lock()
			s := p.statusLocked()
			p.mu.Unlock()
			code := 200
			if r.URL.Path == "/readyz" && (s.Ready == 0 || s.Draining) {
				code = 503
			}
			writeJSON(w, code, s)
		})
	}
	mux.HandleFunc("POST /scan", p.scan)
	return mux
}

func (p *scanPool) scan(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	media, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if media != "application/json" {
		http.Error(w, "use Content-Type: application/json", http.StatusUnsupportedMediaType)
		return
	}
	q, err := synthetic.Decode(http.MaxBytesReader(w, r.Body, 4096))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), scanTimeout)
	defer cancel()
	job := scanJob{ctx: ctx, request: q, start: start, done: make(chan scanOutcome, 1)}
	p.mu.Lock()
	var chosen *scanSlot
	if !p.totals.Draining && ctx.Err() == nil {
		for _, slot := range p.slots {
			if slot.state == "ready" {
				chosen = slot
				slot.state = "busy"
				p.totals.Accepted++
				p.totals.Peak = max(p.totals.Peak, p.statusLocked().Active)
				// Each ready slot has exactly one worker and an empty job channel.
				slot.jobs <- job
				break
			}
		}
	}
	if chosen == nil {
		p.totals.Rejected++
	}
	p.mu.Unlock()
	if chosen == nil {
		w.Header().Set("Retry-After", "1")
		writeJSON(w, 503, map[string]string{"error": "no primed VM available"})
		return
	}
	select {
	case outcome := <-job.done:
		if outcome.err != nil {
			code := http.StatusBadGateway
			if errors.Is(outcome.err, context.DeadlineExceeded) {
				code = http.StatusGatewayTimeout
			}
			writeJSON(w, code, map[string]string{"error": outcome.err.Error()})
			return
		}
		writeJSON(w, 200, outcome.reply)
	case <-ctx.Done():
		writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error": ctx.Err().Error()})
	}
}

func milliseconds(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

func scanGuest(ctx context.Context, d *driver.Driver, transport *vsockhttp.Transport, h substrate.Handle, q synthetic.Request) (synthetic.Result, error) {
	var result synthetic.Result
	body, err := json.Marshal(q)
	if err != nil {
		return result, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://vsock/scan", bytes.NewReader(body))
	if err != nil {
		return result, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := transport.RoundTrip(ctx, d.VsockUDSPath(h.ThreadID), req)
	if err != nil {
		return result, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return result, fmt.Errorf("guest scan returned HTTP %d", resp.StatusCode)
	}
	err = json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&result)
	return result, err
}

func (p *scanPool) worker(ctx context.Context, slot *scanSlot, primes chan struct{}, d *driver.Driver, transport *vsockhttp.Transport, ref substrate.SnapshotRef) (result error) {
	var h substrate.Handle
	release := func() error {
		if h.ID == "" {
			return nil
		}
		if err := d.Release(context.Background(), h); err != nil {
			return err
		}
		thread := h.ThreadID
		h = substrate.Handle{}
		return d.RemoveBundle(thread)
	}
	defer func() {
		result = errors.Join(result, release())
		p.setState(slot, "stopped")
	}()
	for {
		p.mu.Lock()
		draining := p.totals.Draining
		slot.state = "priming"
		p.mu.Unlock()
		if draining || ctx.Err() != nil {
			return nil
		}
		select {
		case primes <- struct{}{}:
		case <-ctx.Done():
			return nil
		}
		start := time.Now()
		primeCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
		var err error
		h, err = d.Claim(primeCtx, substrate.ClaimSpec{Arch: "arm64", BaseSnapshotRef: ref})
		if err == nil {
			err = transport.WaitReady(primeCtx, d.VsockUDSPath(h.ThreadID), "/shim/ready")
		}
		cancel()
		<-primes
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("prime: %w", err)
		}
		restoreMS := milliseconds(time.Since(start))
		p.setState(slot, "ready")
		var job scanJob
		select {
		case job = <-slot.jobs:
		case <-ctx.Done():
			return nil
		}
		start = time.Now()
		scan, scanErr := scanGuest(job.ctx, d, transport, h, job.request)
		reply := scanReply{Result: scan, VMID: h.ID, RestoreMS: restoreMS, GuestMS: milliseconds(time.Since(start))}
		p.setState(slot, "cleaning")
		start = time.Now()
		releaseErr := release()
		reply.CleanupMS = milliseconds(time.Since(start))
		reply.ServerMS = milliseconds(time.Since(job.start))
		outcomeErr := errors.Join(scanErr, releaseErr)
		p.mu.Lock()
		switch {
		case job.ctx.Err() != nil:
			p.totals.Canceled++
		case outcomeErr != nil:
			p.totals.Failed++
		default:
			p.totals.Completed++
		}
		p.mu.Unlock()
		job.done <- scanOutcome{reply: reply, err: outcomeErr}
		log.Printf("scan: vm=%s scan_ms=%.2f total_ms=%.2f error=%v", reply.VMID, reply.ScanMS, reply.ServerMS, outcomeErr)
		if releaseErr != nil {
			return releaseErr
		}
	}
}

func serveScans(ctx context.Context, d *driver.Driver, transport *vsockhttp.Transport, ref substrate.SnapshotRef, count int, address, session string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return errors.New("synthetic development server must listen on a loopback IP")
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	defer listener.Close()
	p := &scanPool{totals: poolStatus{Session: session}}
	for range count {
		p.slots = append(p.slots, &scanSlot{state: "priming", jobs: make(chan scanJob, 1)})
	}
	server := &http.Server{Handler: p.handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 25 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	poolCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	failures := make(chan error, count+1)
	primes := make(chan struct{}, 2)
	var wg sync.WaitGroup
	for _, slot := range p.slots {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := p.worker(poolCtx, slot, primes, d, transport, ref); err != nil {
				failures <- err
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
			failures <- err
		}
	}()
	log.Printf("server: listening on %s with %d fresh-VM slots", address, count)
	var result error
	select {
	case <-ctx.Done():
	case result = <-failures:
	}
	p.mu.Lock()
	p.totals.Draining = true
	p.mu.Unlock()
	log.Printf("server: draining active scans")
	drainCtx, drainCancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := server.Shutdown(drainCtx); err != nil {
		result = errors.Join(result, err)
		_ = server.Close()
	}
	drainCancel()
	cancel()
	wg.Wait()
	close(failures)
	for err := range failures {
		result = errors.Join(result, err)
	}
	if d.LiveCount() != 0 {
		result = errors.Join(result, fmt.Errorf("%d guest processes remain after shutdown", d.LiveCount()))
	}
	log.Printf("server: stopped; live VMs=%d", d.LiveCount())
	return result
}
