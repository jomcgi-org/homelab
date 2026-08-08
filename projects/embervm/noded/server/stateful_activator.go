package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strconv"
	"sync"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

type statefulActivatorFlight struct {
	done  chan struct{}
	entry *statefulEntry
	err   error
}

type statefulInFlightWaitResult uint8

const (
	statefulInFlightSplice statefulInFlightWaitResult = iota
	statefulInFlightWake
	statefulInFlightTimeout
	statefulInFlightCanceled
	// statefulInFlightPending is the poll loop's internal "not decided yet"
	// sentinel and is never returned to a caller. It is deliberately distinct
	// from statefulInFlightWake: folding the two together makes a resolved
	// transition (the token appeared, or the entry was removed) look identical
	// to a transition still in progress, so the wait spins to its deadline
	// instead of handing straight over to the wake path.
	statefulInFlightPending
)

// statefulActivator is noded's node-local opaque-L4 wake path. The accepted
// port identifies the workload, so each eligible workload has exactly one
// in-flight relight and its parked connections independently splice once the
// guest is ready.
type statefulActivator struct {
	server *Server

	mu      sync.Mutex
	flights map[string]*statefulActivatorFlight
	boots   int
	parked  map[string]int
	wakes   []time.Time
}

func newStatefulActivator(s *Server) *statefulActivator {
	return &statefulActivator{
		server:  s,
		flights: make(map[string]*statefulActivatorFlight),
		parked:  make(map[string]int),
	}
}

// StartStatefulActivator starts one accept loop for every already-bound
// stateful L4 listener. Listeners are closed when ctx is cancelled so blocked
// Accept calls exit with the daemon shutdown.
func (s *Server) StartStatefulActivator(ctx context.Context, listeners []net.Listener) {
	if s.statefulActivator == nil || len(listeners) == 0 {
		return
	}
	go func() {
		<-ctx.Done()
		for _, lis := range listeners {
			_ = lis.Close()
		}
	}()
	for _, lis := range listeners {
		port := listenerPort(lis)
		if port == 0 {
			s.logger.Warn("stateful activator: listener has no TCP port", "addr", lis.Addr().String())
			_ = lis.Close()
			continue
		}
		go s.statefulActivator.serve(ctx, lis, port)
	}
}

func listenerPort(lis net.Listener) uint32 {
	if addr, ok := lis.Addr().(*net.TCPAddr); ok && addr.Port > 0 {
		return uint32(addr.Port)
	}
	_, rawPort, err := net.SplitHostPort(lis.Addr().String())
	if err != nil {
		return 0
	}
	port, err := strconv.ParseUint(rawPort, 10, 16)
	if err != nil || port == 0 {
		return 0
	}
	return uint32(port)
}

func (a *statefulActivator) serve(ctx context.Context, lis net.Listener, port uint32) {
	a.server.logger.Info("stateful activator listener listening", "addr", lis.Addr().String(), "port", port)
	for {
		conn, err := lis.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return
			}
			a.server.logger.Warn("stateful activator: accept failed", "port", port, "err", err)
			continue
		}
		go a.handle(ctx, conn, port)
	}
}

func (a *statefulActivator) handle(ctx context.Context, conn net.Conn, listenPort uint32) {
	reg, ok := a.server.registry.statefulByListenPort(listenPort)
	if !ok {
		a.server.logger.Warn("stateful activator: no eligible workload for listener", "port", listenPort)
		_ = conn.Close()
		return
	}

	if live, token, inFlight, ok := a.server.statefulVMs.byWorkloadCheckpoint(reg.Workload); ok && token == "" {
		if !inFlight {
			a.splice(ctx, conn, live)
			return
		}
		live, result := a.waitForInFlight(ctx, reg.Workload)
		switch result {
		case statefulInFlightSplice:
			a.splice(ctx, conn, live)
			return
		case statefulInFlightTimeout:
			a.forwardToControlPlane(ctx, conn, listenPort)
			return
		case statefulInFlightCanceled:
			_ = conn.Close()
			return
		}
	}

	flight, leader, ok := a.join(reg.Workload)
	if !ok {
		a.server.logger.Warn("stateful activator: wake rejected by local limit", "workload", reg.Workload)
		_ = conn.Close()
		return
	}
	defer a.unpark(reg.Workload)

	if leader {
		entry, err := a.wake(ctx, reg)
		a.complete(reg.Workload, flight, entry, err)
	}
	select {
	case <-flight.done:
	case <-ctx.Done():
		_ = conn.Close()
		return
	}
	if flight.err != nil {
		a.server.logger.Warn("stateful activator: wake failed", "workload", reg.Workload, "err", flight.err)
		a.forwardToControlPlane(ctx, conn, listenPort)
		return
	}
	if flight.entry == nil {
		a.server.logger.Warn("stateful activator: wake returned no entry", "workload", reg.Workload)
		_ = conn.Close()
		return
	}
	a.splice(ctx, conn, flight.entry)
}

// join accounts for one parked connection. A follower joins an existing
// workload flight; a leader reserves a globally bounded boot slot and a local
// wake-rate slot.
func (a *statefulActivator) join(workload string) (*statefulActivatorFlight, bool, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.parked[workload] >= activatorMaxParked {
		return nil, false, false
	}
	if f, ok := a.flights[workload]; ok {
		a.parked[workload]++
		return f, false, true
	}
	if a.boots >= activatorMaxBoots || !a.allowWakeLocked(time.Now()) {
		return nil, false, false
	}
	f := &statefulActivatorFlight{done: make(chan struct{})}
	a.flights[workload] = f
	a.boots++
	a.parked[workload]++
	return f, true, true
}

func (a *statefulActivator) allowWakeLocked(now time.Time) bool {
	cutoff := now.Add(-activatorWakeWindow)
	kept := a.wakes[:0]
	for _, woke := range a.wakes {
		if woke.After(cutoff) {
			kept = append(kept, woke)
		}
	}
	a.wakes = kept
	if len(a.wakes) >= activatorWakeMax {
		return false
	}
	a.wakes = append(a.wakes, now)
	return true
}

func (a *statefulActivator) unpark(workload string) {
	a.mu.Lock()
	if a.parked[workload] <= 1 {
		delete(a.parked, workload)
	} else {
		a.parked[workload]--
	}
	a.mu.Unlock()
}

func (a *statefulActivator) complete(workload string, flight *statefulActivatorFlight, entry *statefulEntry, err error) {
	a.mu.Lock()
	flight.entry = entry
	flight.err = err
	delete(a.flights, workload)
	a.boots--
	close(flight.done)
	a.mu.Unlock()
}

// waitForInFlight waits for a stop transition to expose a safe decision. A
// checkpoint token means wake must claim and abort the paused VM, a removed
// entry means wake must relight or cold-boot, and a cleared guard permits the
// existing live VM to be spliced. The bounded fallback preserves the
// node-local activator's escape hatch when a transition gets stuck.
func (a *statefulActivator) waitForInFlight(ctx context.Context, workload string) (*statefulEntry, statefulInFlightWaitResult) {
	deadline := time.NewTimer(activatorInFlightTimeout)
	defer deadline.Stop()
	poll := time.NewTicker(activatorInFlightPollInterval)
	defer poll.Stop()
	check := func() (*statefulEntry, statefulInFlightWaitResult) {
		entry, token, inFlight, ok := a.server.statefulVMs.byWorkloadCheckpoint(workload)
		if !ok || token != "" {
			return nil, statefulInFlightWake
		}
		if !inFlight {
			return entry, statefulInFlightSplice
		}
		return nil, statefulInFlightPending
	}
	if entry, result := check(); result != statefulInFlightPending {
		return entry, result
	}
	for {
		select {
		case <-poll.C:
			if entry, result := check(); result != statefulInFlightPending {
				return entry, result
			}
		case <-deadline.C:
			return nil, statefulInFlightTimeout
		case <-ctx.Done():
			return nil, statefulInFlightCanceled
		}
	}
}

func (a *statefulActivator) wake(ctx context.Context, reg workloadEntry) (*statefulEntry, error) {
	for {
		live, token, inFlight, ok := a.server.statefulVMs.byWorkloadCheckpoint(reg.Workload)
		if !ok {
			break
		}
		if token == "" {
			if !inFlight {
				return live, nil
			}
			resumed, result := a.waitForInFlight(ctx, reg.Workload)
			switch result {
			case statefulInFlightSplice:
				return resumed, nil
			case statefulInFlightTimeout:
				return nil, fmt.Errorf("noded: stateful workload %q remained in transition", reg.Workload)
			case statefulInFlightCanceled:
				return nil, ctx.Err()
			}
			continue
		}
		e, claimed := a.server.statefulVMs.claimResolve(live.vmID, token)
		if !claimed {
			if resumed, resumedToken, resumedInFlight, found := a.server.statefulVMs.byWorkloadCheckpoint(reg.Workload); found && resumedToken == "" && !resumedInFlight {
				return resumed, nil
			}
			return nil, fmt.Errorf("noded: checkpoint resolve raced for stateful workload %q", reg.Workload)
		}
		if _, err := a.server.abortCheckpoint(ctx, e, token, 0); err != nil {
			return nil, err
		}
		resumed, resumedToken, resumedInFlight, ok := a.server.statefulVMs.byWorkloadCheckpoint(reg.Workload)
		if !ok || resumedToken != "" || resumedInFlight {
			return nil, fmt.Errorf("noded: checkpoint abort for stateful workload %q did not restore a live VM", reg.Workload)
		}
		return resumed, nil
	}

	// Resolve boot_image_ref from the daemon's OWN base registry by workload (the
	// control plane supplies it per-call today, but the activator has no control
	// plane during a gap; the base key is node-local so it cannot ride the global
	// workload registry). Threaded into BOTH modes so a RELIGHT that falls back to
	// a cold boot (generation mismatch or an unreadable ledger) still has a base.
	// A COLD wake with no ready base fails cleanly (the workload stays dark until
	// the control plane returns), the accepted node-local degradation.
	bootImageRef, _ := a.server.bases.readyByWorkload(reg.Workload)

	req := &nodev1.StartStatefulRequest{
		Trace:             &nodev1.Trace{Workload: reg.Workload},
		Port:              reg.StatefulPort,
		BootImageRef:      bootImageRef,
		VolumeMount:       reg.StatefulVolumeMount,
		VolumeDevice:      reg.StatefulVolumeDevice,
		BlessedGeneration: 0,
		Resources:         &nodev1.ResourceSpec{Vcpus: reg.VCPUs, MemMib: reg.MemMib},
	}
	if bundle, ok := a.server.statefulBundles.byWorkload(reg.Workload); ok {
		req.Mode = nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT
		req.RelightSnapshotRef = bundle.snapshotRef
	} else {
		req.Mode = nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD
	}
	if _, err := a.server.startStateful(ctx, req, nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR); err != nil {
		return nil, err
	}
	entry, ok := a.server.statefulVMs.byWorkload(reg.Workload)
	if !ok {
		return nil, fmt.Errorf("noded: activated stateful VM for workload %q was not registered", reg.Workload)
	}
	return entry, nil
}

func (a *statefulActivator) splice(ctx context.Context, client net.Conn, entry *statefulEntry) {
	if entry == nil || entry.ip == nil || entry.port == 0 {
		_ = client.Close()
		return
	}
	address := net.JoinHostPort(entry.ip.String(), strconv.FormatUint(uint64(entry.port), 10))
	if err := spliceTCP(ctx, client, address); err != nil {
		a.server.logger.Warn("stateful activator: guest dial failed", "workload", entry.workload, "err", err)
		_ = client.Close()
	}
}

func (a *statefulActivator) forwardToControlPlane(ctx context.Context, client net.Conn, listenPort uint32) {
	ip := a.server.registry.controlPlaneActivator()
	if ip == "" {
		_ = client.Close()
		return
	}
	address := net.JoinHostPort(ip, strconv.FormatUint(uint64(listenPort), 10))
	if err := spliceTCP(ctx, client, address); err != nil {
		a.server.logger.Warn("stateful activator: control-plane forward dial failed", "address", address, "err", err)
		_ = client.Close()
	}
}
