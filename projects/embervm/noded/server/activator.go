package server

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

const (
	activatorMaxBoots   = 16
	activatorMaxParked  = 64
	activatorWakeMax    = 30
	activatorWakeWindow = time.Minute
	activatorBodyMax    = 8 << 20
	// The transition wait is short enough to notice a checkpoint resolve
	// promptly, while the deadline prevents a stuck VM from pinning a client.
	activatorInFlightPollInterval = 25 * time.Millisecond
	activatorInFlightTimeout      = 10 * time.Second
)

// A guest that is paused can leave its tap reachable while swallowing SYNs, so
// an unbounded dial sits on kernel SYN retransmit (~127s) and the caller reads
// it as a hang. Bound only the dial so an unreachable guest fails fast; once
// connected, a splice is a long-lived connection and must not be capped by this
// timeout. A var rather than a const purely so the test can shrink it: proving
// the bound requires a context with NO deadline of its own (a context that
// already carries one would be honoured by the unbounded dial too, and the test
// would pass against the very regression it exists to catch).
var activatorDialTimeout = 10 * time.Second

var activatorDeniedHeaders = map[string]struct{}{
	"content-length":      {},
	"transfer-encoding":   {},
	"connection":          {},
	"keep-alive":          {},
	"upgrade":             {},
	"host":                {},
	"te":                  {},
	"trailer":             {},
	"proxy-authorization": {},
	"proxy-authenticate":  {},
}

type activatorFlight struct {
	done  chan struct{}
	entry *servingEntry
	err   error
}

// activator is noded's node-local serving wake path. A workload has exactly one
// in-flight start; requests that arrive while it starts wait for that result and
// independently proxy their buffered request once the guest is ready.
type activator struct {
	server *Server
	client *http.Client

	mu      sync.Mutex
	flights map[string]*activatorFlight
	boots   int
	parked  int
	wakes   []time.Time
}

func newActivator(s *Server) *activator {
	return &activator{
		server:  s,
		client:  &http.Client{},
		flights: make(map[string]*activatorFlight),
	}
}

// ActivatorHandler returns the node-local HTTP activator surface.
func (s *Server) ActivatorHandler() http.Handler {
	return s.activator
}

// EnableActivator marks the activator listening only after cmd/main has bound
// its socket. This keeps a pre-listen NodeStatus from advertising a black hole.
func (s *Server) EnableActivator() {
	s.activatorMu.Lock()
	s.activatorEnabled = true
	s.activatorMu.Unlock()
	s.signalChange()
}

func (a *activator) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	workload := strings.TrimSpace(r.Header.Get("x-ember-workload"))
	if workload == "" {
		http.Error(w, "x-ember-workload required", http.StatusBadRequest)
		return
	}
	reg, ok := a.server.registry.get(workload)
	if !ok || !reg.NodeLocalWake {
		a.server.logger.Warn("activator: workload is not eligible for node-local wake", "workload", workload)
		http.Error(w, "node-local wake unavailable", http.StatusServiceUnavailable)
		return
	}

	// Straggler splice: if a VM is already live for this workload, proxy straight
	// through instead of waking. There is a narrow window where a request passes
	// this check while no VM is live, the prior leader then completes and deletes
	// its flight, and this request becomes a fresh leader and boots a second VM.
	// That is bounded by the node live-VM cap and the workload max-instances and
	// reaped by idle-bank, so it self-heals; Phase 1 tolerates it rather than
	// holding a lock across the whole boot.
	if live, ok := a.server.servingVMs.firstByWorkload(workload); ok {
		a.proxy(w, r, live)
		return
	}

	body, err := readActivatorBody(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
		return
	}

	flight, leader, code := a.join(workload)
	if code != 0 {
		a.server.logger.Warn("activator: wake rejected by local limit", "workload", workload, "status", code)
		http.Error(w, "activator busy", code)
		return
	}
	defer a.unpark()

	if leader {
		entry, bootErr := a.boot(workload, reg)
		a.complete(workload, flight, entry, bootErr)
	}
	select {
	case <-flight.done:
	case <-r.Context().Done():
		return
	}
	if flight.err != nil || flight.entry == nil {
		code := http.StatusBadGateway
		if status.Code(flight.err) == codes.Unavailable || status.Code(flight.err) == codes.ResourceExhausted {
			code = http.StatusServiceUnavailable
		}
		a.server.logger.Warn("activator: serving wake failed", "workload", workload, "err", flight.err)
		http.Error(w, "serving wake failed", code)
		return
	}
	a.proxyBuffered(w, r, body, flight.entry)
}

// join accounts for one parked request and either joins the workload's existing
// boot or reserves the globally bounded wake slot for its new leader.
func (a *activator) join(workload string) (*activatorFlight, bool, int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.parked >= activatorMaxParked {
		return nil, false, http.StatusServiceUnavailable
	}
	if f, ok := a.flights[workload]; ok {
		a.parked++
		return f, false, 0
	}
	if a.boots >= activatorMaxBoots || !a.allowWakeLocked(time.Now()) {
		return nil, false, http.StatusServiceUnavailable
	}
	f := &activatorFlight{done: make(chan struct{})}
	a.flights[workload] = f
	a.boots++
	a.parked++
	return f, true, 0
}

func (a *activator) allowWakeLocked(now time.Time) bool {
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

func (a *activator) unpark() {
	a.mu.Lock()
	a.parked--
	a.mu.Unlock()
}

func (a *activator) complete(workload string, flight *activatorFlight, entry *servingEntry, err error) {
	a.mu.Lock()
	flight.entry = entry
	flight.err = err
	delete(a.flights, workload)
	a.boots--
	close(flight.done)
	a.mu.Unlock()
}

func (a *activator) boot(workload string, reg workloadEntry) (*servingEntry, error) {
	req := &nodev1.StartServingRequest{
		Trace:      &nodev1.Trace{Workload: workload},
		Port:       reg.ServingPort,
		HealthPath: reg.ServingHealthPath,
		Resources:  &nodev1.ResourceSpec{Vcpus: reg.VCPUs, MemMib: reg.MemMib},
	}
	if snap, ok := a.server.servingSnap.freshestByWorkload(workload); ok {
		req.Source = &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: snap.snapshotRef}}
	} else {
		image, ok := a.servingImage(workload)
		if !ok {
			return nil, fmt.Errorf("noded: no serving image provisioned for workload %q", workload)
		}
		req.Source = &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: image.baseKey}}
	}
	if _, err := a.server.startServing(context.Background(), req, nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR); err != nil {
		return nil, err
	}
	entry, ok := a.server.servingVMs.firstByWorkload(workload)
	if !ok {
		return nil, fmt.Errorf("noded: activated serving vm for workload %q was not registered", workload)
	}
	return entry, nil
}

func (a *activator) servingImage(workload string) (servingImageEntry, bool) {
	var selected servingImageEntry
	found := false
	for _, image := range a.server.servingImage.snapshot() {
		if image.workload != workload || !a.server.imageProvisioned(image.runtimeImageRef) {
			continue
		}
		if !found || image.baseKey > selected.baseKey {
			selected, found = image, true
		}
	}
	return selected, found
}

func readActivatorBody(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	body, err := io.ReadAll(io.LimitReader(r.Body, activatorBodyMax+1))
	if err != nil {
		return nil, err
	}
	if len(body) > activatorBodyMax {
		return nil, fmt.Errorf("request body exceeds %d byte limit", activatorBodyMax)
	}
	return body, nil
}

func (a *activator) proxy(w http.ResponseWriter, r *http.Request, entry *servingEntry) {
	body, err := readActivatorBody(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
		return
	}
	a.proxyBuffered(w, r, body, entry)
}

func (a *activator) proxyBuffered(w http.ResponseWriter, r *http.Request, body []byte, entry *servingEntry) {
	if entry == nil || entry.ip == nil || entry.port == 0 {
		http.Error(w, "serving endpoint unavailable", http.StatusBadGateway)
		return
	}
	path := r.URL.RequestURI()
	if path == "" {
		path = "/"
	}
	target := "http://" + net.JoinHostPort(entry.ip.String(), strconv.FormatUint(uint64(entry.port), 10)) + path
	forward, err := http.NewRequestWithContext(r.Context(), r.Method, target, bytes.NewReader(body))
	if err != nil {
		http.Error(w, "invalid serving request", http.StatusBadRequest)
		return
	}
	forward.Header = allowedHeaders(r.Header)
	resp, err := a.client.Do(forward)
	if err != nil {
		http.Error(w, "serving endpoint unavailable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	for key, values := range allowedHeaders(resp.Header) {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func allowedHeaders(headers http.Header) http.Header {
	allowed := make(http.Header, len(headers))
	for key, values := range headers {
		if _, denied := activatorDeniedHeaders[strings.ToLower(key)]; denied {
			continue
		}
		allowed[key] = append([]string(nil), values...)
	}
	return allowed
}

func spliceTCP(ctx context.Context, client net.Conn, address string) error {
	dialCtx, cancel := context.WithTimeout(ctx, activatorDialTimeout)
	defer cancel()
	guest, err := (&net.Dialer{}).DialContext(dialCtx, "tcp", address)
	if err != nil {
		return err
	}
	defer client.Close()
	defer guest.Close()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		pumpTCP(guest, client)
	}()
	go func() {
		defer wg.Done()
		pumpTCP(client, guest)
	}()
	wg.Wait()
	return nil
}

func pumpTCP(dst, src net.Conn) {
	_, _ = io.Copy(dst, src)
	if conn, ok := dst.(*net.TCPConn); ok {
		_ = conn.CloseWrite()
		return
	}
	_ = dst.Close()
}
