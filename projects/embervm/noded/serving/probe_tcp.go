package serving

import (
	"context"
	"net"
	"strconv"
	"time"
)

// defaultTCPProbeTimeout bounds a single TCP CONNECT probe so a hung guest
// cannot stall the probe loop past one interval, mirroring defaultProbeTimeout
// for the HTTP prober.
const defaultTCPProbeTimeout = 3 * time.Second

// TCPProber runs the per-VM health-probe loop for a stateful VM (R4): unlike
// Prober's HTTP GET, a stateful guest answers opaque L4 TCP (decision 4 of the
// R4 contract), so health is TCP CONNECT success, not a status code. It reuses
// the SAME threshold state machine (healthState) and ProbeHandle plumbing as
// the HTTP prober, so NodeStatus.stateful_vms.healthy follows identical
// unhealthy-threshold/one-success-recovers semantics to serving_vms.healthy;
// only the probe mechanic differs.
type TCPProber struct {
	timeout   time.Duration
	interval  time.Duration
	threshold int
}

// NewTCPProber builds a TCPProber with the given interval and unhealthy
// threshold, applying defaults for zero values exactly like NewProber.
func NewTCPProber(interval time.Duration, threshold int) *TCPProber {
	if interval <= 0 {
		interval = DefaultProbeInterval
	}
	if threshold < 1 {
		threshold = DefaultUnhealthyThreshold
	}
	timeout := defaultTCPProbeTimeout
	if timeout >= interval {
		timeout = interval / 2
	}
	return &TCPProber{timeout: timeout, interval: interval, threshold: threshold}
}

// Start runs the TCP-connect probe loop until ctx is cancelled (StopStateful/
// Destroy cancels it). It dials ip:port every interval, folds each outcome
// through the threshold state machine, and calls publish with the current
// verdict after every probe. Like Prober.Start it does NOT probe immediately:
// the VM was already health-gated ready by StartStateful's waitStatefulReady,
// so the first loop tick is one interval out.
func (p *TCPProber) Start(ctx context.Context, ip net.IP, port uint32, publish func(ProbeResult)) {
	state := newHealthState(p.threshold)
	addr := net.JoinHostPort(ip.String(), strconv.FormatUint(uint64(port), 10))
	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			ok := p.probeOnce(addr)
			healthy := state.record(ok)
			publish(ProbeResult{Healthy: healthy, LastProbeUnixMs: time.Now().UnixMilli()})
		}
	}
}

// probeOnce dials addr with the prober's timeout and reports whether a TCP
// connection was established. Success closes the connection immediately: the
// probe is connectivity-only, it never speaks the guest's application
// protocol (which is workload-opaque, e.g. Postgres wire format).
func (p *TCPProber) probeOnce(addr string) bool {
	conn, err := net.DialTimeout("tcp", addr, p.timeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// StartTCPProbe launches a TCP prober goroutine for a stateful VM and returns a
// handle, mirroring StartProbe. The handle's latest result starts HEALTHY (the
// VM was health-gated ready before this) so the first NodeStatus after
// StartStateful reports healthy without waiting a full interval.
func StartTCPProbe(prober *TCPProber, ip net.IP, port uint32) *ProbeHandle {
	ctx, cancel := context.WithCancel(context.Background())
	h := &ProbeHandle{cancel: cancel, result: ProbeResult{Healthy: true, LastProbeUnixMs: time.Now().UnixMilli()}}
	go prober.Start(ctx, ip, port, h.set)
	return h
}
