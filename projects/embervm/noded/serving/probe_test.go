package serving

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestHealthStateFlipThresholds drives the pure threshold state machine through
// sequences of probe outcomes and asserts the flip semantics: healthy flips false
// only after unhealthy_threshold CONSECUTIVE failures, and back to true after a
// single success.
func TestHealthStateFlipThresholds(t *testing.T) {
	tests := []struct {
		name      string
		threshold int
		outcomes  []bool // probe results in order
		want      []bool // expected healthy verdict after each outcome
	}{
		{
			name:      "starts healthy, two failures below threshold stay healthy",
			threshold: 3,
			outcomes:  []bool{false, false},
			want:      []bool{true, true},
		},
		{
			name:      "third consecutive failure flips unhealthy",
			threshold: 3,
			outcomes:  []bool{false, false, false},
			want:      []bool{true, true, false},
		},
		{
			name:      "single success recovers immediately",
			threshold: 3,
			outcomes:  []bool{false, false, false, true},
			want:      []bool{true, true, false, true},
		},
		{
			name:      "success resets the consecutive-failure count",
			threshold: 3,
			outcomes:  []bool{false, false, true, false, false},
			want:      []bool{true, true, true, true, true}, // never reached 3 in a row
		},
		{
			name:      "threshold of one flips on first failure",
			threshold: 1,
			outcomes:  []bool{false, true, false},
			want:      []bool{false, true, false},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			st := newHealthState(tt.threshold)
			for i, ok := range tt.outcomes {
				got := st.record(ok)
				if got != tt.want[i] {
					t.Errorf("after outcome %d (ok=%v): healthy=%v want %v", i, ok, got, tt.want[i])
				}
			}
		})
	}
}

func TestNewHealthStateDefaultsThreshold(t *testing.T) {
	st := newHealthState(0)
	if st.unhealthyThreshold != DefaultUnhealthyThreshold {
		t.Errorf("zero threshold not defaulted: got %d want %d", st.unhealthyThreshold, DefaultUnhealthyThreshold)
	}
	if !st.healthy {
		t.Error("a fresh health state should start healthy")
	}
}

func TestProbeURL(t *testing.T) {
	tests := []struct {
		ip   string
		port uint32
		path string
		want string
	}{
		{"172.31.0.2", 8080, "/healthz", "http://172.31.0.2:8080/healthz"},
		{"172.31.0.2", 80, "healthz", "http://172.31.0.2:80/healthz"}, // path normalised to leading slash
		{"172.31.0.9", 3000, "", "http://172.31.0.9:3000/"},           // empty path -> "/"
	}
	for _, tt := range tests {
		got := probeURL(net.ParseIP(tt.ip), tt.port, tt.path)
		if got != tt.want {
			t.Errorf("probeURL(%s,%d,%q) = %q want %q", tt.ip, tt.port, tt.path, got, tt.want)
		}
	}
}

func TestProbeHandleStartsHealthy(t *testing.T) {
	// StartProbe reports healthy immediately (the VM was health-gated ready before the
	// loop began), without waiting a full interval.
	prober := NewProber(0, 0) // defaults
	h := StartProbe(prober, net.ParseIP("172.31.0.2"), 8080, "/healthz")
	defer h.Stop()
	if !h.Result().Healthy {
		t.Error("a freshly started probe handle should report healthy")
	}
	if h.Result().LastProbeUnixMs == 0 {
		t.Error("a freshly started probe handle should carry a last-probe timestamp")
	}
}

func TestProbeOnceReportsHTTPAndTransportFailures(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/ready" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	prober := NewProber(0, 0)
	if !prober.probeOnce(context.Background(), server.URL+"/ready") {
		t.Error("2xx health response should be healthy")
	}
	if prober.probeOnce(context.Background(), server.URL+"/unready") {
		t.Error("non-2xx health response should be unhealthy")
	}
	server.Close()
	if prober.probeOnce(context.Background(), server.URL+"/ready") {
		t.Error("connection failure should be unhealthy")
	}
}
