package server

import (
	"context"
	"io"
	"net"
	"strconv"
	"testing"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

func statefulActivatorEchoServer(t *testing.T) uint32 {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("stateful echo listen: %v", err)
	}
	t.Cleanup(func() { _ = lis.Close() })
	go func() {
		for {
			conn, err := lis.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	_, rawPort, err := net.SplitHostPort(lis.Addr().String())
	if err != nil {
		t.Fatalf("stateful echo split port: %v", err)
	}
	port, err := strconv.ParseUint(rawPort, 10, 32)
	if err != nil {
		t.Fatalf("stateful echo parse port: %v", err)
	}
	return uint32(port)
}

func enableStatefulActivatorWorkload(s *Server, workload string, listenPort, guestPort uint32) {
	s.registry.sync([]workloadEntry{{
		Workload:             workload,
		NodeLocalWake:        true,
		StatefulListenPort:   listenPort,
		StatefulPort:         guestPort,
		StatefulVolumeMount:  "/var/lib/postgresql/data",
		StatefulBootImageRef: "img-a",
		VCPUs:                1,
		MemMib:               128,
	}})
}

func startStatefulActivator(t *testing.T, s *Server) uint32 {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("stateful activator listen: %v", err)
	}
	s.StartStatefulActivator(ctx, []net.Listener{lis})
	return listenerPort(lis)
}

func statefulActivatorConn(t *testing.T, listenPort uint32) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.FormatUint(uint64(listenPort), 10)), 2*time.Second)
	if err != nil {
		t.Fatalf("stateful activator dial: %v", err)
	}
	return conn
}

func statefulActivatorRoundTrip(t *testing.T, conn net.Conn, body string) {
	t.Helper()
	if err := conn.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		t.Fatalf("set deadline: %v", err)
	}
	if _, err := conn.Write([]byte(body)); err != nil {
		t.Fatalf("activator write: %v", err)
	}
	got := make([]byte, len(body))
	if _, err := io.ReadFull(conn, got); err != nil {
		t.Fatalf("activator read: %v", err)
	}
	if string(got) != body {
		t.Fatalf("activator bytes = %q, want %q", got, body)
	}
}

func addStatefulActivatorBundle(t *testing.T, s *Server, driver *fakeStatefulDriver, workload, ref string) {
	t.Helper()
	if err := s.volumes.Create(workload, 1<<20); err != nil {
		t.Fatalf("create stateful volume: %v", err)
	}
	driver.mu.Lock()
	driver.banked[ref] = 0
	driver.mu.Unlock()
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: workload, generation: 0})
}

// waitForStatefulActivatorParked polls the stateful activator's parked count for
// the workload under the mutex until it reaches expected, or times out. This ensures
// the test does not release the restore handler before all concurrent clients have
// actually been admitted and are parked waiting for that release, rather than
// being scheduled but not yet in the stateful activator's join() critical section.
func waitForStatefulActivatorParked(t *testing.T, a *statefulActivator, workload string, expected int) {
	t.Helper()
	timeout := time.Now().Add(5 * time.Second)
	for {
		a.mu.Lock()
		current := a.parked[workload]
		a.mu.Unlock()
		if current >= expected {
			return
		}
		if time.Now().After(timeout) {
			t.Fatalf("timeout waiting for %d parked clients on %q, got %d", expected, workload, current)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestStatefulActivatorColdBootResolvesBaseLocally(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	// A volume exists but there is NO banked bundle, so the activator must COLD-boot.
	// The COLD path needs boot_image_ref, which the control plane does not push (it
	// is node-local); the activator resolves it from the daemon's own base registry
	// (readyByWorkload). The harness seeds a READY base "img-a" for "wl-state".
	if err := s.volumes.Create("wl-state", 1<<20); err != nil {
		t.Fatalf("create stateful volume: %v", err)
	}

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "cold-hello")

	driver.mu.Lock()
	claims := driver.claims
	driver.mu.Unlock()
	if claims != 1 {
		t.Errorf("ClaimStateful calls = %d, want 1 (one cold boot)", claims)
	}
	status := s.statefulVMsStatus()
	if len(status) != 1 || status[0].GetOrigin() != nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR {
		t.Errorf("expected one ACTIVATOR-origin stateful VM, got %+v", status)
	}
}

func TestStatefulActivatorStragglerSplicesLiveVM(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	s.statefulVMs.add(&statefulEntry{vmID: "already-live", workload: "wl-state", ip: net.ParseIP("127.0.0.1"), port: port})

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "live bytes")
	if driver.claims != 0 || driver.restores != 0 {
		t.Errorf("stateful wake calls = claims:%d restores:%d, want 0:0", driver.claims, driver.restores)
	}
}

func TestStatefulActivatorRelightsLocalBundle(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	addStatefulActivatorBundle(t, s, driver, "wl-state", "bundle-state")

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "relit bytes")
	if driver.restores != 1 {
		t.Errorf("RestoreStateful calls = %d, want 1", driver.restores)
	}
	if got := s.statefulVMsStatus()[0].GetOrigin(); got != nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR {
		t.Errorf("stateful origin = %v, want ACTIVATOR", got)
	}
}

func TestStatefulActivatorSingleFlight(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	addStatefulActivatorBundle(t, s, driver, "wl-state", "bundle-state")
	driver.restoreStarted = make(chan struct{}, 1)
	driver.releaseRestore = make(chan struct{})

	const clients = 8
	conns := make(chan net.Conn, clients)
	for i := 0; i < clients; i++ {
		conns <- statefulActivatorConn(t, listenPort)
	}
	<-driver.restoreStarted
	// Wait for all 8 clients to reach the stateful activator and be parked. restoreStarted
	// only proves the FIRST restore started; stragglers may not yet have called join()
	// and incremented a.parked[workload]. Do not release the restore gate until all
	// are actually admitted, so the single-flight assertion holds.
	waitForStatefulActivatorParked(t, s.statefulActivator, "wl-state", clients)
	close(driver.releaseRestore)
	for i := 0; i < clients; i++ {
		conn := <-conns
		statefulActivatorRoundTrip(t, conn, "all clients")
		_ = conn.Close()
	}
	if driver.restores != 1 {
		t.Errorf("RestoreStateful calls = %d, want 1", driver.restores)
	}
}

func TestStatefulActivatorGateClosesIneligiblePort(t *testing.T) {
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	s.registry.sync([]workloadEntry{{Workload: "wl-state", NodeLocalWake: false, StatefulListenPort: listenPort}})
	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("ineligible stateful activator connection stayed open")
	}
	if driver.claims != 0 || driver.restores != 0 {
		t.Errorf("stateful wake calls = claims:%d restores:%d, want 0:0", driver.claims, driver.restores)
	}
}

func TestStatefulActivatorAbortsCheckpointAndSplices(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)
	started := startFreshStateful(t, s, port, "wl-state")
	before, err := s.volumes.Generation("wl-state")
	if err != nil {
		t.Fatalf("generation before checkpoint: %v", err)
	}
	if _, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_CHECKPOINT,
	}); err != nil {
		t.Fatalf("StopStateful(checkpoint): %v", err)
	}

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "resumed bytes")
	after, err := s.volumes.Generation("wl-state")
	if err != nil {
		t.Fatalf("generation after checkpoint abort: %v", err)
	}
	if after != before+1 {
		t.Errorf("generation after checkpoint abort = %d, want %d", after, before+1)
	}
	if driver.resumes != 1 {
		t.Errorf("ResolveStatefulAbort calls = %d, want 1", driver.resumes)
	}
}

func TestStatefulActivatorAndControlPlaneOrigins(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	enableStatefulActivatorWorkload(s, "activator-workload", listenPort, port)
	addStatefulActivatorBundle(t, s, driver, "activator-workload", "bundle-activator")

	conn := statefulActivatorConn(t, listenPort)
	statefulActivatorRoundTrip(t, conn, "activator origin")
	_ = conn.Close()
	if got := s.statefulVMsStatus()[0].GetOrigin(); got != nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR {
		t.Errorf("activator origin = %v, want ACTIVATOR", got)
	}
	if _, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: s.statefulVMsStatus()[0].GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY,
	}); err != nil {
		t.Fatalf("StopStateful(destroy): %v", err)
	}
	startFreshStateful(t, s, port, "cp-workload")
	for _, vm := range s.statefulVMsStatus() {
		if vm.GetWorkload() == "cp-workload" && vm.GetOrigin() != nodev1.InstanceOrigin_INSTANCE_ORIGIN_CONTROL_PLANE {
			t.Errorf("control-plane origin = %v, want CONTROL_PLANE", vm.GetOrigin())
		}
	}
}

func TestStatefulActivatorWakeFailureForwardsToControlPlane(t *testing.T) {
	port := statefulActivatorEchoServer(t)
	s, _, driver := newStatefulTestServer(t)
	listenPort := startStatefulActivator(t, s)
	controlPlaneIP, cpAccepted := controlPlaneActivatorEchoServer(t, listenPort)
	s.registry.setControlPlaneActivator(controlPlaneIP)
	enableStatefulActivatorWorkload(s, "wl-state", listenPort, port)

	conn := statefulActivatorConn(t, listenPort)
	defer conn.Close()
	statefulActivatorRoundTrip(t, conn, "forwarded stateful bytes")
	select {
	case <-cpAccepted:
	case <-time.After(2 * time.Second):
		t.Fatal("control-plane activator did not receive the failed stateful wake")
	}
	if driver.claims != 0 || driver.restores != 0 {
		t.Errorf("stateful wake calls = claims:%d restores:%d, want 0:0", driver.claims, driver.restores)
	}
}

func TestStatefulActivatorWaitsForInFlightDecision(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*statefulRegistry, *statefulEntry)
	}{
		{
			name: "token appears and wakes",
			mutate: func(reg *statefulRegistry, entry *statefulEntry) {
				reg.markCheckpointed(entry.vmID, "checkpoint-token")
			},
		},
		{
			name: "entry disappears and wakes",
			mutate: func(reg *statefulRegistry, entry *statefulEntry) {
				reg.remove(entry.vmID)
			},
		},
		{
			name: "guard clears and splices",
			mutate: func(reg *statefulRegistry, entry *statefulEntry) {
				reg.clearInFlight(entry.vmID)
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s, _, _ := newStatefulTestServer(t)
			entry := &statefulEntry{vmID: "vm-in-flight", workload: "wl-state"}
			s.statefulVMs.add(entry)
			if _, ok := s.statefulVMs.beginStop(entry.vmID); !ok {
				t.Fatal("beginStop failed")
			}
			a := newStatefulActivator(s)
			go func() {
				time.Sleep(2 * activatorInFlightPollInterval)
				tt.mutate(s.statefulVMs, entry)
			}()

			got, result := a.waitForInFlight(context.Background(), entry.workload)
			want := statefulInFlightWake
			if tt.name == "guard clears and splices" {
				want = statefulInFlightSplice
				if got != entry {
					t.Fatalf("wait entry = %p, want %p", got, entry)
				}
			}
			if result != want {
				t.Fatalf("wait result = %v, want %v", result, want)
			}
		})
	}
}

func TestSpliceTCPBoundsGuestDial(t *testing.T) {
	client, peer := net.Pipe()
	t.Cleanup(func() {
		_ = client.Close()
		_ = peer.Close()
	})

	// context.Background() deliberately carries NO deadline, so the only thing
	// that can bound this dial is activatorDialTimeout itself. Passing a context
	// that already had a deadline would be honoured by an unbounded dial too, so
	// the test would pass against the regression it exists to catch. 192.0.2.1
	// is TEST-NET-1: it black-holes the SYN rather than refusing it, which is
	// what a paused guest does.
	restore := activatorDialTimeout
	activatorDialTimeout = 150 * time.Millisecond
	t.Cleanup(func() { activatorDialTimeout = restore })

	started := time.Now()
	err := spliceTCP(context.Background(), client, "192.0.2.1:5432")
	if err == nil {
		t.Fatal("spliceTCP succeeded against a black-hole address")
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("spliceTCP dial took %s, want it bounded by activatorDialTimeout", elapsed)
	}
}
