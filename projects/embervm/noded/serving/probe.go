package serving

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"
)

// Default probe knobs (overridable via daemon config).
const (
	// DefaultProbeInterval is how often the daemon probes each live serving VM.
	DefaultProbeInterval = 5 * time.Second
	// DefaultUnhealthyThreshold is the number of CONSECUTIVE probe failures that
	// flips a VM healthy->false. One success flips it back.
	DefaultUnhealthyThreshold = 3
	// defaultProbeTimeout bounds a single health GET so a hung guest cannot stall the
	// probe loop past one interval.
	defaultProbeTimeout = 3 * time.Second
)

// healthState is the pure threshold state machine for one serving VM's health. It
// flips healthy=false only after unhealthyThreshold CONSECUTIVE failures and back to
// true after a single success, exactly per the Task 1 contract. It is a pure value
// type (no I/O, no clock) so the flip semantics are unit-tested directly by feeding a
// sequence of probe outcomes.
type healthState struct {
	unhealthyThreshold int
	healthy            bool
	consecutiveFails   int
}

// newHealthState starts a VM optimistically HEALTHY: a serving VM only enters the
// probe loop after StartServing has already health-gated it ready over the tap, so
// its first reported state should be healthy until it fails threshold times.
func newHealthState(unhealthyThreshold int) *healthState {
	if unhealthyThreshold < 1 {
		unhealthyThreshold = DefaultUnhealthyThreshold
	}
	return &healthState{unhealthyThreshold: unhealthyThreshold, healthy: true}
}

// record folds one probe outcome into the state and returns the resulting healthy
// verdict. A success resets the consecutive-failure count and marks healthy; a
// failure increments and flips to unhealthy only once the threshold is reached (so
// transient single failures below the threshold do not flap the verdict).
func (h *healthState) record(ok bool) bool {
	if ok {
		h.consecutiveFails = 0
		h.healthy = true
		return h.healthy
	}
	h.consecutiveFails++
	if h.consecutiveFails >= h.unhealthyThreshold {
		h.healthy = false
	}
	return h.healthy
}

// ProbeResult is the health fact the daemon REPORTS in NodeStatus.serving_vms. The
// daemon never acts on it (no restart, no eviction): the control plane consumes it.
type ProbeResult struct {
	Healthy         bool
	LastProbeUnixMs int64
}

// Prober runs the per-VM health-probe loop. One Prober is started per live serving
// VM at StartServing and stopped at StopServing (via the context passed to Start).
// The verdict is published to a callback the server wires into the serving registry,
// so NodeStatus reads the latest health under the registry lock, never here.
type Prober struct {
	client    *http.Client
	interval  time.Duration
	threshold int
	timeout   time.Duration
}

// NewProber builds a Prober with the given interval and unhealthy threshold, applying
// defaults for zero values. The HTTP client's per-request timeout bounds a single
// probe below the interval.
func NewProber(interval time.Duration, threshold int) *Prober {
	if interval <= 0 {
		interval = DefaultProbeInterval
	}
	if threshold < 1 {
		threshold = DefaultUnhealthyThreshold
	}
	timeout := defaultProbeTimeout
	if timeout >= interval {
		timeout = interval / 2
	}
	return &Prober{
		client:    &http.Client{Timeout: timeout},
		interval:  interval,
		threshold: threshold,
		timeout:   timeout,
	}
}

// Freshness returns the maximum age for a probe fact before a caller should
// require a new observation. It covers one normal cadence plus the bounded
// request timeout, so a healthy VM is not expired between scheduled probes.
func (p *Prober) Freshness() time.Duration {
	return p.interval + p.timeout
}

// Start runs the probe loop until ctx is cancelled (StopServing/Destroy cancels it).
// It probes GET http://ip:port{healthPath} every interval, folds each outcome through
// the threshold state machine, and calls publish with the current verdict after every
// probe so NodeStatus always reflects the latest fact. Start blocks; callers run it in
// a goroutine. It does NOT probe immediately: the VM was already health-gated ready by
// StartServing, so the first loop tick is one interval out.
func (p *Prober) Start(ctx context.Context, ip net.IP, port uint32, healthPath string, publish func(ProbeResult)) {
	state := newHealthState(p.threshold)
	url := probeURL(ip, port, healthPath)
	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			ok := p.probeOnce(ctx, url)
			healthy := state.record(ok)
			publish(ProbeResult{Healthy: healthy, LastProbeUnixMs: time.Now().UnixMilli()})
		}
	}
}

// probeOnce performs one health GET and reports whether it returned a 2xx.
func (p *Prober) probeOnce(ctx context.Context, url string) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// probeURL builds the health-probe URL for a VM's tap IP:port and health path. The
// path is normalised to start with a slash.
func probeURL(ip net.IP, port uint32, healthPath string) string {
	if healthPath == "" {
		healthPath = "/"
	}
	if healthPath[0] != '/' {
		healthPath = "/" + healthPath
	}
	return fmt.Sprintf("http://%s:%d%s", ip.String(), port, healthPath)
}

// ---- probe handle used by the server registry -------------------------------

// ProbeHandle bundles a running prober's cancel func with its latest published
// result, so the serving registry can stop the loop on teardown and read the current
// health without racing the goroutine. It is safe for concurrent use.
type ProbeHandle struct {
	cancel context.CancelFunc
	done   chan struct{}
	fresh  time.Duration

	mu     sync.Mutex
	result ProbeResult
}

// StartProbe launches a prober goroutine for a VM and returns a handle. The handle's
// latest result starts HEALTHY (the VM was health-gated ready before this) so the
// first NodeStatus after StartServing reports healthy without waiting a full interval.
func StartProbe(prober *Prober, ip net.IP, port uint32, healthPath string) *ProbeHandle {
	ctx, cancel := context.WithCancel(context.Background())
	h := &ProbeHandle{
		cancel: cancel,
		done:   make(chan struct{}),
		fresh:  prober.Freshness(),
		result: ProbeResult{Healthy: true, LastProbeUnixMs: time.Now().UnixMilli()},
	}
	go func() {
		defer close(h.done)
		prober.Start(ctx, ip, port, healthPath, h.set)
	}()
	return h
}

// set stores the latest probe result under the handle lock.
func (h *ProbeHandle) set(r ProbeResult) {
	h.mu.Lock()
	h.result = r
	h.mu.Unlock()
}

// Result reads the latest published health fact.
func (h *ProbeHandle) Result() ProbeResult {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.result
}

// Freshness returns the configured age bound for the probe facts in this
// handle.
func (h *ProbeHandle) Freshness() time.Duration {
	if h == nil {
		return 0
	}
	return h.fresh
}

// Done returns a channel that closes after the probe loop has stopped.
func (h *ProbeHandle) Done() <-chan struct{} {
	if h == nil {
		return nil
	}
	return h.done
}

// Stop cancels the probe goroutine (idempotent).
func (h *ProbeHandle) Stop() {
	if h != nil && h.cancel != nil {
		h.cancel()
	}
}
