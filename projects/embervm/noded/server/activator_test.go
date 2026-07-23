package server

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

type activatorRoundTripper func(*http.Request) (*http.Response, error)

func (f activatorRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func activatorGuest(t *testing.T, handler http.Handler) (uint32, *httptest.Server) {
	t.Helper()
	guest := httptest.NewServer(handler)
	t.Cleanup(guest.Close)
	_, rawPort, err := net.SplitHostPort(guest.Listener.Addr().String())
	if err != nil {
		t.Fatalf("guest port: %v", err)
	}
	port, err := strconv.ParseUint(rawPort, 10, 32)
	if err != nil {
		t.Fatalf("parse guest port: %v", err)
	}
	return uint32(port), guest
}

func enableActivatorWorkload(s *Server, workload string, port uint32) {
	s.registry.sync([]workloadEntry{{
		Workload:          workload,
		NodeLocalWake:     true,
		ServingPort:       port,
		ServingHealthPath: defaultReadyPath,
		VCPUs:             1,
		MemMib:            128,
	}})
}

func activatorRequest(t *testing.T, handler http.Handler, workload, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewBufferString(body))
	req.Header.Set("x-ember-workload", workload)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}

func TestActivatorStragglerProxiesLiveVM(t *testing.T) {
	var calls int
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path == defaultReadyPath {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("live"))
	}))
	s, _, driver := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-serve", port)
	s.servingVMs.add(&servingEntry{vmID: "already-live", workload: "wl-serve", ip: net.ParseIP("127.0.0.1"), port: port})

	rec := activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request")
	if rec.Code != http.StatusOK || rec.Body.String() != "live" {
		t.Fatalf("activator response = %d %q, want 200 live", rec.Code, rec.Body.String())
	}
	if driver.claims != 0 {
		t.Errorf("ClaimServing calls = %d, want 0", driver.claims)
	}
	if calls != 1 {
		t.Errorf("guest calls = %d, want 1", calls)
	}
}

func TestActivatorColdBootAndProxyFiltersHeaders(t *testing.T) {
	var gotBody string
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == defaultReadyPath {
			w.WriteHeader(http.StatusOK)
			return
		}
		body, _ := io.ReadAll(r.Body)
		gotBody = string(body)
		w.Header().Set("Connection", "keep-alive")
		w.Header().Set("X-Guest", "ok")
		_, _ = w.Write([]byte("guest response"))
	}))
	s, _, driver := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-serve", port)
	var forwarded http.Header
	s.activator.client = &http.Client{Transport: activatorRoundTripper(func(r *http.Request) (*http.Response, error) {
		forwarded = r.Header.Clone()
		return http.DefaultTransport.RoundTrip(r)
	})}
	req := httptest.NewRequest(http.MethodPost, "/invoke", bytes.NewBufferString("request body"))
	req.Header.Set("x-ember-workload", "wl-serve")
	for denied := range activatorDeniedHeaders {
		req.Header.Set(denied, "removed")
	}
	rec := httptest.NewRecorder()
	s.ActivatorHandler().ServeHTTP(rec, req)

	if rec.Code != http.StatusOK || rec.Body.String() != "guest response" {
		t.Fatalf("activator response = %d %q, want 200 guest response", rec.Code, rec.Body.String())
	}
	if driver.claims != 1 {
		t.Errorf("ClaimServing calls = %d, want 1", driver.claims)
	}
	if gotBody != "request body" {
		t.Errorf("guest body = %q, want request body", gotBody)
	}
	for denied := range activatorDeniedHeaders {
		if forwarded.Get(denied) != "" {
			t.Errorf("forwarded deny-listed header %q = %q", denied, forwarded.Get(denied))
		}
	}
	if rec.Header().Get("Connection") != "" || rec.Header().Get("Content-Length") != "" {
		t.Errorf("deny-listed response headers leaked: %+v", rec.Header())
	}
	if rec.Header().Get("X-Guest") != "ok" {
		t.Errorf("X-Guest = %q, want ok", rec.Header().Get("X-Guest"))
	}
}

func TestActivatorDenyListFiltersBothDirections(t *testing.T) {
	headers := make(http.Header, len(activatorDeniedHeaders)+1)
	for denied := range activatorDeniedHeaders {
		headers.Set(denied, "removed")
	}
	headers.Set("X-Allowed", "kept")
	for key := range activatorDeniedHeaders {
		if got := allowedHeaders(headers).Get(key); got != "" {
			t.Errorf("allowedHeaders retained %q = %q", key, got)
		}
	}
	if got := allowedHeaders(headers).Get("X-Allowed"); got != "kept" {
		t.Errorf("allowed header = %q, want kept", got)
	}
}

func TestNodeStatusAdvertisesBoundActivator(t *testing.T) {
	s, _, _ := newServingTestServer(t)
	// The activator is advertised at the daemon's own POD IP (ADR embervm/018 as
	// amended for the pod-networked daemon), the address a pod-network Envoy dials.
	s.cfg.PodIP = "10.42.0.9"
	s.cfg.ActivatorPort = 8081
	// Not advertised until the listener is bound (EnableActivator), so NodeStatus
	// never points Envoy at a listener that is not up yet.
	if got := s.nodeStatus().GetActivatorEndpoint(); got != nil {
		t.Fatalf("activator endpoint before bind = %+v, want nil", got)
	}
	s.EnableActivator()
	ns := s.nodeStatus()
	if got := ns.GetActivatorEndpoint(); got == nil || got.GetIp() != "10.42.0.9" || got.GetPort() != 8081 {
		t.Errorf("activator endpoint = %+v, want 10.42.0.9:8081", got)
	}
	if ns.GetActivatorIp() != "10.42.0.9" {
		t.Errorf("activator ip = %q, want 10.42.0.9", ns.GetActivatorIp())
	}
	// No pod IP (local/test): advertise nothing, so the control plane keeps its own
	// activator address as the fallback.
	s.cfg.PodIP = ""
	if got := s.nodeStatus().GetActivatorEndpoint(); got != nil {
		t.Errorf("activator endpoint with no pod IP = %+v, want nil", got)
	}
}

func TestActivatorSingleFlight(t *testing.T) {
	healthStarted := make(chan struct{})
	releaseHealth := make(chan struct{})
	var healthOnce sync.Once
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == defaultReadyPath {
			healthOnce.Do(func() { close(healthStarted) })
			<-releaseHealth
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("all served"))
	}))
	s, _, driver := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-serve", port)
	const requests = 8
	responses := make(chan *httptest.ResponseRecorder, requests)
	for i := 0; i < requests; i++ {
		go func() {
			req := httptest.NewRequest(http.MethodPost, "/invoke", bytes.NewBufferString("request"))
			req.Header.Set("x-ember-workload", "wl-serve")
			rec := httptest.NewRecorder()
			s.ActivatorHandler().ServeHTTP(rec, req)
			responses <- rec
		}()
	}
	<-healthStarted
	close(releaseHealth)
	for i := 0; i < requests; i++ {
		rec := <-responses
		if rec.Code != http.StatusOK || rec.Body.String() != "all served" {
			t.Errorf("activator response = %d %q, want 200 all served", rec.Code, rec.Body.String())
		}
	}
	if driver.claims != 1 {
		t.Errorf("ClaimServing calls = %d, want 1", driver.claims)
	}
}

func TestActivatorGateRejectsIneligibleWorkload(t *testing.T) {
	s, _, driver := newServingTestServer(t)
	s.registry.sync([]workloadEntry{{Workload: "wl-serve", NodeLocalWake: false}})
	rec := activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request")
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503", rec.Code)
	}
	if driver.claims != 0 {
		t.Errorf("ClaimServing calls = %d, want 0", driver.claims)
	}
}

func TestActivatorWakeRateLimit(t *testing.T) {
	s, _, _ := newServingTestServer(t)
	handler := s.ActivatorHandler()
	for i := 0; i < activatorWakeMax; i++ {
		workload := "wl-" + strconv.Itoa(i)
		s.registry.sync([]workloadEntry{{Workload: workload, NodeLocalWake: true, ServingPort: 8080}})
		rec := activatorRequest(t, handler, workload, "/invoke", "request")
		if rec.Code != http.StatusBadGateway {
			t.Fatalf("wake %d status = %d, want 502", i, rec.Code)
		}
	}
	s.registry.sync([]workloadEntry{{Workload: "over-limit", NodeLocalWake: true, ServingPort: 8080}})
	rec := activatorRequest(t, handler, "over-limit", "/invoke", "request")
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("over-limit status = %d, want 503", rec.Code)
	}
}

func TestActivatorAndControlPlaneOrigins(t *testing.T) {
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == defaultReadyPath {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("ok"))
	}))
	s, _, _ := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-serve", port)
	if rec := activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request"); rec.Code != http.StatusOK {
		t.Fatalf("activator status = %d, want 200", rec.Code)
	}
	if got := s.servingVMsStatus()[0].GetOrigin(); got != nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR {
		t.Errorf("activator origin = %v, want ACTIVATOR", got)
	}
	s.servingImage.add(servingImageEntry{baseKey: "cp-image", workload: "cp-workload", handlerPath: "/handler", runtimeImageRef: "img-a"})
	if _, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Trace:      &nodev1.Trace{Workload: "cp-workload"},
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "cp-image"}},
		Port:       port,
		HealthPath: defaultReadyPath,
	}); err != nil {
		t.Fatalf("StartServing: %v", err)
	}
	for _, vm := range s.servingVMsStatus() {
		if vm.GetWorkload() == "cp-workload" && vm.GetOrigin() != nodev1.InstanceOrigin_INSTANCE_ORIGIN_CONTROL_PLANE {
			t.Errorf("control-plane origin = %v, want CONTROL_PLANE", vm.GetOrigin())
		}
	}
}
