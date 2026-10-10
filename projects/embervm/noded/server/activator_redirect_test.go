package server

import (
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
)

// TestActivatorRelaysGuestRedirectWithoutFollowing pins the SSRF posture: a guest
// answering the relayed request with a 3xx whose Location points at another host
// gets that 3xx relayed to the caller, and the daemon's client never dials the
// Location target. Following it would let a tap guest aim noded at the pod
// network or at another tenant's VM with the body returned to the caller.
func TestActivatorRelaysGuestRedirectWithoutFollowing(t *testing.T) {
	var victimHits atomic.Int32
	victim := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		victimHits.Add(1)
		_, _ = w.Write([]byte("internal secret"))
	}))
	t.Cleanup(victim.Close)

	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == defaultReadyPath {
			w.WriteHeader(http.StatusOK)
			return
		}
		http.Redirect(w, r, victim.URL+"/admin", http.StatusFound)
	}))
	s, _, _ := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-redirect", port)
	probe := serving.StartProbe(serving.NewProber(5*time.Millisecond, 1), net.ParseIP("127.0.0.1"), port, defaultReadyPath)
	t.Cleanup(func() {
		probe.Stop()
		<-probe.Done()
	})
	s.servingVMs.add(&servingEntry{vmID: "redirecting", workload: "wl-redirect", ip: net.ParseIP("127.0.0.1"), port: port, probe: probe})
	time.Sleep(15 * time.Millisecond)

	rec := activatorRequest(t, s.ActivatorHandler(), "wl-redirect", "/invoke", "request")
	if rec.Code != http.StatusFound {
		t.Fatalf("activator response = %d, want the guest's 302 relayed", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != victim.URL+"/admin" {
		t.Errorf("Location = %q, want the guest's header relayed verbatim", got)
	}
	if rec.Body.String() == "internal secret" {
		t.Fatal("activator relayed the redirect target's body: the redirect was followed")
	}
	if n := victimHits.Load(); n != 0 {
		t.Fatalf("redirect target was dialled %d times, want 0", n)
	}
}

// TestProberTreatsRedirectAsUnhealthy pins that a guest redirecting its health path
// never counts as healthy and never has its Location followed.
func TestProberTreatsRedirectAsUnhealthy(t *testing.T) {
	var victimHits atomic.Int32
	victim := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		victimHits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(victim.Close)
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, victim.URL+"/healthz", http.StatusFound)
	}))
	probe := serving.StartProbe(serving.NewProber(5*time.Millisecond, 1), net.ParseIP("127.0.0.1"), port, defaultReadyPath)
	t.Cleanup(func() {
		probe.Stop()
		<-probe.Done()
	})
	time.Sleep(30 * time.Millisecond)
	if probe.Result().Healthy {
		t.Fatal("a redirecting health path was judged healthy")
	}
	if n := victimHits.Load(); n != 0 {
		t.Fatalf("probe followed the redirect %d times, want 0", n)
	}
}
