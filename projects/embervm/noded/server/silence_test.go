package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/volume"
)

// silenceBound is the armed ADR embervm/037 bound used across these tests. The
// value itself is irrelevant: tests age contact with setLastContactForTest, so
// nothing ever sleeps out the bound.
const silenceBound = 21600

// armSilence turns the silence gate on for an already-built test server.
func armSilence(s *Server) {
	s.cfg.SilenceTimeoutSeconds = silenceBound
}

// agedContact moves a server's last contact past the armed bound.
func agedContact(s *Server) {
	s.setLastContactForTest(time.Now().Add(-(silenceBound + 100) * time.Second))
}

func TestSilencedPredicate(t *testing.T) {
	t.Run("timeout zero disables even when never contacted", func(t *testing.T) {
		s := registerTestServer(config.Config{})
		if s.silenced() {
			t.Fatal("silenced() = true with the gate disabled")
		}
	})
	t.Run("armed and never contacted is silent", func(t *testing.T) {
		s := registerTestServer(config.Config{SilenceTimeoutSeconds: silenceBound})
		if !s.silenced() {
			t.Fatal("silenced() = false before the first contact (boot case)")
		}
	})
	t.Run("fresh contact is not silent", func(t *testing.T) {
		s := registerTestServer(config.Config{SilenceTimeoutSeconds: silenceBound})
		s.noteContact()
		if s.silenced() {
			t.Fatal("silenced() = true with fresh contact")
		}
	})
	t.Run("contact older than the bound is silent", func(t *testing.T) {
		s := registerTestServer(config.Config{SilenceTimeoutSeconds: silenceBound})
		agedContact(s)
		if !s.silenced() {
			t.Fatal("silenced() = false past the bound")
		}
	})
}

// register-2xx-refreshes-contact: only a successful dial-home POST refreshes
// the silence clock; a rejected one leaves the brick silent.
func TestRegister2xxRefreshesContact(t *testing.T) {
	newSilentServer := func() *Server {
		s := registerTestServer(config.Config{
			Node:                  "node-4",
			ControlPlaneURL:       "http://cp:8080",
			SilenceTimeoutSeconds: silenceBound,
		})
		agedContact(s)
		return s
	}

	ok := newSilentServer()
	if err := ok.register(context.Background(), &fakeDoer{status: http.StatusOK}, "boot"); err != nil {
		t.Fatalf("register: %v", err)
	}
	if ok.silenced() {
		t.Error("a 2xx dial-home POST did not refresh the silence clock")
	}

	rejected := newSilentServer()
	if err := rejected.register(context.Background(), &fakeDoer{status: http.StatusForbidden}, "boot"); err == nil {
		t.Fatal("expected error on 403")
	}
	if !rejected.silenced() {
		t.Error("a non-2xx dial-home POST refreshed the silence clock")
	}
}

// watchnode-send-refreshes-contact: every successful NodeStatus send counts as
// control-plane contact, so a streaming brick never goes silent while it talks.
func TestWatchNodeSendRefreshesContact(t *testing.T) {
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: t.TempDir(), SilenceTimeoutSeconds: silenceBound},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	s.slotCeiling = func(configured uint64) uint64 { return configured }
	agedContact(s)
	if !s.silenced() {
		t.Fatal("precondition: server should start past the bound")
	}

	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	nodev1.RegisterNodeServiceServer(gs, s)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client := nodev1.NewNodeServiceClient(conn)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	stream, err := client.WatchNode(ctx, &nodev1.WatchNodeRequest{NodeId: "node-4"})
	if err != nil {
		t.Fatalf("WatchNode: %v", err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("Recv initial: %v", err)
	}
	if s.silenced() {
		t.Error("a successful WatchNode send did not refresh the silence clock")
	}
}

// activator-silence-refuses-wake: a silenced brick answers the L7 activator
// with 503 naming the gate and boots nothing; fresh contact wakes normally.
func TestActivatorRefusesWakeWhenSilenced(t *testing.T) {
	port, _ := activatorGuest(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == defaultReadyPath {
			w.WriteHeader(http.StatusOK)
			return
		}
		_, _ = w.Write([]byte("ok"))
	}))
	s, _, driver := newServingTestServer(t)
	enableActivatorWorkload(s, "wl-serve", port)
	armSilence(s)

	rec := activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request")
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "silence") {
		t.Fatalf("silenced wake response = %d %q, want 503 naming the silence gate", rec.Code, rec.Body.String())
	}
	if driver.claims != 0 {
		t.Errorf("ClaimServing calls while silenced = %d, want 0", driver.claims)
	}

	s.noteContact()
	rec = activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request")
	if rec.Code != http.StatusOK || rec.Body.String() != "ok" {
		t.Fatalf("fresh-contact wake response = %d %q, want 200 ok", rec.Code, rec.Body.String())
	}
	if driver.claims != 1 {
		t.Errorf("ClaimServing calls after contact = %d, want 1", driver.claims)
	}
}

// stateful-activator-silence-refuses-wake: a silenced brick closes the L4 wake
// connection without starting anything; fresh contact relights the local bundle.
func TestStatefulActivatorRefusesWakeWhenSilenced(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	addStatefulActivatorBundle(t, s, driver, "wl-state", "bundle-state")
	armSilence(s)

	conn := statefulActivatorConn(t, listenPort)
	func() {
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
		if _, err := conn.Read(make([]byte, 1)); err == nil {
			t.Fatal("silenced stateful activator connection stayed open")
		}
	}()
	driver.mu.Lock()
	claims, restores := driver.claims, driver.restores
	driver.mu.Unlock()
	if claims != 0 || restores != 0 {
		t.Errorf("stateful wake while silenced = claims:%d restores:%d, want 0:0", claims, restores)
	}

	s.noteContact()
	conn = statefulActivatorConn(t, listenPort)
	statefulActivatorRoundTrip(t, conn, "relit bytes")
	driver.mu.Lock()
	restores = driver.restores
	driver.mu.Unlock()
	if restores != 1 {
		t.Errorf("RestoreStateful calls after contact = %d, want 1", restores)
	}
}

// group-activator-silence-refuses-start: a silenced brick starts no group set;
// fresh contact relights the complete local set.
func TestGroupActivatorRefusesStartWhenSilenced(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	addCompleteGroupActivatorSet(s, driver, port)
	armSilence(s)

	conn := groupActivatorConn(t, listenPort)
	assertGroupActivatorClosed(t, conn)
	driver.mu.Lock()
	restores := driver.restores
	driver.mu.Unlock()
	if restores != 0 {
		t.Errorf("RestoreGroupMember calls while silenced = %d, want 0", restores)
	}

	s.noteContact()
	conn = groupActivatorConn(t, listenPort)
	groupActivatorRoundTrip(t, conn, "relit group bytes")
	driver.mu.Lock()
	restores = driver.restores
	driver.mu.Unlock()
	if restores != 3 {
		t.Errorf("RestoreGroupMember calls after contact = %d, want 3", restores)
	}
}

// attachGeneration-silence-scoping: silence blocks ONLY the activator-origin
// blessing-lease self-advance; the control-plane-issued blessed_generation
// path stays open, and fresh contact restores the self-advance. A seeded lease
// pins the gate's position: if the check ever moved BELOW ConsumeGenerationFromLease,
// the refused call would silently consume a lease generation and the recovery
// attach below would return the NEXT generation instead of the lease's first.
func TestAttachGenerationSilenceScoping(t *testing.T) {
	s, _, _ := newStatefulTestServer(t)
	armSilence(s)
	agedContact(s)
	if err := s.volumes.Create("wl-state", 1<<20); err != nil {
		t.Fatalf("create stateful volume: %v", err)
	}
	if err := s.volumes.ApplyBlessingLease("wl-state", volume.BlessingLease{NextGeneration: 10, LeaseEnd: 12}); err != nil {
		t.Fatalf("seed blessing lease: %v", err)
	}
	before, err := s.volumes.Generation("wl-state")
	if err != nil {
		t.Fatalf("generation before: %v", err)
	}

	_, err = s.attachGeneration("wl-state", 0, true)
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(status.Convert(err).Message(), "silence") {
		t.Fatalf("silenced self-advance err = %v, want FailedPrecondition naming the silence gate", err)
	}
	after, err := s.volumes.Generation("wl-state")
	if err != nil {
		t.Fatalf("generation after refused self-advance: %v", err)
	}
	if after != before {
		t.Errorf("generation advanced while silenced: %d -> %d", before, after)
	}

	blessed, err := s.attachGeneration("wl-state", 5, false)
	if err != nil {
		t.Fatalf("blessed_generation attach while silenced: %v", err)
	}
	if blessed != 5 {
		t.Errorf("blessed_generation attach recorded %d, want 5", blessed)
	}

	s.noteContact()
	gen, err := s.attachGeneration("wl-state", 0, true)
	if err != nil {
		t.Fatalf("self-advance after contact: %v", err)
	}
	// Exactly the seeded lease's first generation: proves the refused call
	// consumed nothing from the lease while silenced.
	if gen != 10 {
		t.Errorf("self-advance after contact recorded %d, want 10 (the seeded lease's first slot)", gen)
	}
}

// activator-silence-splices-live-vm: silence refuses NEW work but must not
// disturb an already-live VM. If a regression moves the gate above the
// live-splice shortcut, this fails with a 503 instead of a spliced response.
func TestActivatorLiveVMSplicesWhileSilenced(t *testing.T) {
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
	armSilence(s)
	agedContact(s)

	rec := activatorRequest(t, s.ActivatorHandler(), "wl-serve", "/invoke", "request")
	if rec.Code != http.StatusOK || rec.Body.String() != "live" {
		t.Fatalf("silenced splice response = %d %q, want 200 live", rec.Code, rec.Body.String())
	}
	if driver.claims != 0 {
		t.Errorf("ClaimServing calls while silenced = %d, want 0", driver.claims)
	}
	if calls != 1 {
		t.Errorf("guest calls = %d, want 1", calls)
	}
}

// stateful-activator-silence-splices-live-vm: the L4 mirror of the test above.
// A silenced brick keeps serving an already-live stateful VM and starts nothing.
func TestStatefulActivatorLiveVMSplicesWhileSilenced(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	s.statefulVMs.add(&statefulEntry{vmID: "already-live", workload: "wl-state", ip: net.ParseIP("127.0.0.1"), port: port})
	armSilence(s)
	agedContact(s)

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "live bytes")
	driver.mu.Lock()
	claims, restores := driver.claims, driver.restores
	driver.mu.Unlock()
	if claims != 0 || restores != 0 {
		t.Errorf("stateful wake calls while silenced = claims:%d restores:%d, want 0:0", claims, restores)
	}
}
