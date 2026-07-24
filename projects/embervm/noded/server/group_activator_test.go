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

func groupActivatorEchoServer(t *testing.T) uint32 {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("group echo listen: %v", err)
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
		t.Fatalf("group echo split port: %v", err)
	}
	port, err := strconv.ParseUint(rawPort, 10, 32)
	if err != nil {
		t.Fatalf("group echo parse port: %v", err)
	}
	return uint32(port)
}

func startGroupActivator(t *testing.T, s *Server) uint32 {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("group activator listen: %v", err)
	}
	s.StartGroupActivator(ctx, []net.Listener{lis})
	return listenerPort(lis)
}

func groupActivatorConn(t *testing.T, listenPort uint32) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.FormatUint(uint64(listenPort), 10)), 2*time.Second)
	if err != nil {
		t.Fatalf("group activator dial: %v", err)
	}
	return conn
}

func groupActivatorRoundTrip(t *testing.T, conn net.Conn, body string) {
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

func groupActivatorPlan(port uint32) []groupMemberPlanEntry {
	return []groupMemberPlanEntry{
		{MemberName: "server", MemberIndex: 0, StartOrder: 0, HealthPort: port, EntryGuestPort: port, ReadyBudgetSeconds: 30, VCPUs: 1, MemMib: 128},
		{MemberName: "agent-0", MemberIndex: 1, StartOrder: 1, HealthPort: port, ReadyBudgetSeconds: 30, VCPUs: 1, MemMib: 128},
		{MemberName: "agent-1", MemberIndex: 2, StartOrder: 1, HealthPort: port, ReadyBudgetSeconds: 30, VCPUs: 1, MemMib: 128},
	}
}

func enableGroupActivatorWorkload(s *Server, workload string, listenPort, guestPort uint32) {
	s.registry.sync([]workloadEntry{{
		Workload:        workload,
		NodeLocalWake:   true,
		GroupListenPort: listenPort,
		GroupMemberPlan: groupActivatorPlan(guestPort),
	}})
}

func addGroupActivatorBundle(s *Server, driver *fakeGroupMemberDriver, setID, groupInstanceID, memberName, pinnedIP string, port uint32) {
	driver.mu.Lock()
	driver.banked[setID+"/"+memberName] = true
	driver.mu.Unlock()
	s.groupBundles.add(groupBundleEntry{
		setID:           setID,
		memberName:      memberName,
		groupInstanceID: groupInstanceID,
		snapshotRef:     "group/" + setID + "/" + memberName,
		pinnedIP:        pinnedIP,
		port:            port,
	})
}

func addCompleteGroupActivatorSet(s *Server, driver *fakeGroupMemberDriver, port uint32) {
	for _, member := range groupActivatorPlan(port) {
		addGroupActivatorBundle(s, driver, "set-1", "grp-A", member.MemberName, "127.0.0.1", member.HealthPort)
	}
}

func assertGroupActivatorClosed(t *testing.T, conn net.Conn) {
	t.Helper()
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := conn.Read(make([]byte, 1)); err == nil {
		t.Fatal("group activator connection stayed open")
	}
}

func controlPlaneActivatorEchoServer(t *testing.T, listenPort uint32) (string, <-chan struct{}) {
	t.Helper()
	const ip = "127.0.0.2"
	lis, err := net.Listen("tcp", net.JoinHostPort(ip, strconv.FormatUint(uint64(listenPort), 10)))
	if err != nil {
		t.Fatalf("control-plane activator listen: %v", err)
	}
	t.Cleanup(func() { _ = lis.Close() })
	accepted := make(chan struct{}, 1)
	go func() {
		for {
			conn, err := lis.Accept()
			if err != nil {
				return
			}
			select {
			case accepted <- struct{}{}:
			default:
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	return ip, accepted
}

func assertNoControlPlaneForward(t *testing.T, accepted <-chan struct{}) {
	t.Helper()
	select {
	case <-accepted:
		t.Fatal("connection was forwarded to the control-plane activator")
	case <-time.After(100 * time.Millisecond):
	}
}

func TestGroupActivatorRelightRequestCarriesReadyBudget(t *testing.T) {
	req := groupActivatorRelightRequest("scratch-k8s", "grp-A", groupRelightMember{
		plan: groupMemberPlanEntry{
			MemberName:         "server",
			ReadyBudgetSeconds: 47,
		},
		bundle: groupBundleEntry{
			pinnedIP:    "127.0.0.1",
			snapshotRef: "group/set-1/server",
		},
	})
	if got := req.GetReadyBudgetSeconds(); got != 47 {
		t.Errorf("relight ready budget = %d, want 47", got)
	}
}

func TestGroupActivatorStragglerSplicesLiveEntry(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	controlPlaneIP, cpAccepted := controlPlaneActivatorEchoServer(t, listenPort)
	s.registry.setControlPlaneActivator(controlPlaneIP)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	s.groupMembers.add(&groupMemberEntry{
		vmID:            "already-live",
		workload:        "scratch-k8s",
		groupInstanceID: "grp-A",
		memberName:      "server",
		ip:              net.ParseIP("127.0.0.1"),
		isEntry:         true,
		entryGuestPort:  port,
	})

	conn := groupActivatorConn(t, listenPort)
	defer conn.Close()
	groupActivatorRoundTrip(t, conn, "live group bytes")
	driver.mu.Lock()
	if driver.restores != 0 || driver.claims != 0 {
		t.Errorf("group wake calls = claims:%d restores:%d, want 0:0", driver.claims, driver.restores)
	}
	driver.mu.Unlock()
	assertNoControlPlaneForward(t, cpAccepted)
}

func TestGroupActivatorRelightsCompleteSetInRoleOrder(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, clock := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	addCompleteGroupActivatorSet(s, driver, port)

	conn := groupActivatorConn(t, listenPort)
	defer conn.Close()
	groupActivatorRoundTrip(t, conn, "relit group bytes")

	driver.mu.Lock()
	restores := driver.restores
	order := append([]string(nil), driver.restoreOrder...)
	driver.mu.Unlock()
	if restores != 3 {
		t.Fatalf("RestoreGroupMember calls = %d, want 3", restores)
	}
	if len(order) != 3 || order[0] != "server" {
		t.Errorf("restore order = %v, want server before both agents", order)
	}
	status := s.groupMemberVmsStatus()
	if len(status) != 3 {
		t.Fatalf("live group members = %d, want 3", len(status))
	}
	for _, member := range status {
		if member.GetOrigin() != nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR {
			t.Errorf("member %q origin = %v, want ACTIVATOR", member.GetMemberName(), member.GetOrigin())
		}
	}
	clock.mu.Lock()
	deadlines := append([]time.Time(nil), clock.deadlines...)
	clock.mu.Unlock()
	if len(deadlines) != 3 {
		t.Fatalf("clock resync deadlines = %d, want 3", len(deadlines))
	}
	for _, deadline := range deadlines {
		if remaining := time.Until(deadline); remaining < 20*time.Second {
			t.Errorf("clock resync deadline remaining = %v, want plan budget near 30s", remaining)
		}
	}
}

func TestGroupActivatorSingleFlight(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	addCompleteGroupActivatorSet(s, driver, port)
	driver.restoreStarted = make(chan struct{}, 1)
	driver.releaseRestore = make(chan struct{})

	const clients = 8
	conns := make([]net.Conn, 0, clients)
	for i := 0; i < clients; i++ {
		conns = append(conns, groupActivatorConn(t, listenPort))
	}
	<-driver.restoreStarted
	close(driver.releaseRestore)
	for _, conn := range conns {
		groupActivatorRoundTrip(t, conn, "all group clients")
		_ = conn.Close()
	}
	driver.mu.Lock()
	restores := driver.restores
	driver.mu.Unlock()
	if restores != 3 {
		t.Errorf("RestoreGroupMember calls = %d, want exactly one three-member set", restores)
	}
}

func TestGroupActivatorIncompleteAndLegacySetsClose(t *testing.T) {
	tests := []struct {
		name string
		seed func(*Server, *fakeGroupMemberDriver, uint32)
	}{
		{
			name: "missing member",
			seed: func(s *Server, driver *fakeGroupMemberDriver, port uint32) {
				addGroupActivatorBundle(s, driver, "set-1", "grp-A", "server", "127.0.0.1", port)
				addGroupActivatorBundle(s, driver, "set-1", "grp-A", "agent-0", "127.0.0.1", port)
			},
		},
		{
			name: "missing pinned IP",
			seed: func(s *Server, driver *fakeGroupMemberDriver, port uint32) {
				addCompleteGroupActivatorSet(s, driver, port)
				s.groupBundles.add(groupBundleEntry{
					setID: "set-1", memberName: "agent-1", groupInstanceID: "grp-A",
					snapshotRef: "group/set-1/agent-1",
				})
			},
		},
		{
			name: "members span sets",
			seed: func(s *Server, driver *fakeGroupMemberDriver, port uint32) {
				addGroupActivatorBundle(s, driver, "set-1", "grp-A", "server", "127.0.0.1", port)
				addGroupActivatorBundle(s, driver, "set-2", "grp-A", "agent-0", "127.0.0.1", port)
				addGroupActivatorBundle(s, driver, "set-2", "grp-A", "agent-1", "127.0.0.1", port)
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			port := groupActivatorEchoServer(t)
			s, _, driver, _ := newGroupMemberTestServer(t)
			listenPort := startGroupActivator(t, s)
			enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
			tc.seed(s, driver, port)

			assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
			driver.mu.Lock()
			restores := driver.restores
			driver.mu.Unlock()
			if restores != 0 {
				t.Errorf("RestoreGroupMember calls = %d, want 0", restores)
			}
		})
	}
}

func TestGroupActivatorGateClosesIneligiblePort(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	s.registry.sync([]workloadEntry{{
		Workload:        "scratch-k8s",
		NodeLocalWake:   false,
		GroupListenPort: listenPort,
		GroupMemberPlan: groupActivatorPlan(port),
	}})
	addCompleteGroupActivatorSet(s, driver, port)

	assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if driver.restores != 0 {
		t.Errorf("RestoreGroupMember calls = %d, want 0", driver.restores)
	}
}

func TestGroupActivatorRelightFailureReapsStartedMembers(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	addCompleteGroupActivatorSet(s, driver, port)
	driver.failRestoreMember = "agent-1"

	assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
	if got := s.groupMembers.count(); got != 0 {
		t.Errorf("live group members after failed relight = %d, want 0", got)
	}
	if got := len(s.groupBundles.snapshot()); got != 3 {
		t.Errorf("banked bundles after failed relight = %d, want 3", got)
	}
}

func TestGroupActivatorWakeFailureForwardsToControlPlane(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, driver, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	controlPlaneIP, cpAccepted := controlPlaneActivatorEchoServer(t, listenPort)
	s.registry.setControlPlaneActivator(controlPlaneIP)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)

	conn := groupActivatorConn(t, listenPort)
	defer conn.Close()
	groupActivatorRoundTrip(t, conn, "forwarded group bytes")
	select {
	case <-cpAccepted:
	case <-time.After(2 * time.Second):
		t.Fatal("control-plane activator did not receive the failed group wake")
	}
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if driver.restores != 0 {
		t.Errorf("RestoreGroupMember calls = %d, want 0 for incomplete set", driver.restores)
	}
}

func TestGroupActivatorWakeFailureWithoutControlPlaneAddressCloses(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)

	assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
}

func TestGroupActivatorWakeFailureControlPlaneDialFailureCloses(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	const controlPlaneIP = "127.0.0.2"
	probe, err := net.Listen("tcp", net.JoinHostPort(controlPlaneIP, strconv.FormatUint(uint64(listenPort), 10)))
	if err != nil {
		t.Fatalf("reserve control-plane activator address: %v", err)
	}
	_ = probe.Close()
	s.registry.setControlPlaneActivator(controlPlaneIP)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)

	assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
}

func TestGroupActivatorRateLimitDoesNotForward(t *testing.T) {
	port := groupActivatorEchoServer(t)
	s, _, _, _ := newGroupMemberTestServer(t)
	listenPort := startGroupActivator(t, s)
	controlPlaneIP, cpAccepted := controlPlaneActivatorEchoServer(t, listenPort)
	s.registry.setControlPlaneActivator(controlPlaneIP)
	enableGroupActivatorWorkload(s, "scratch-k8s", listenPort, port)
	s.groupActivator.mu.Lock()
	for i := 0; i < activatorWakeMax; i++ {
		s.groupActivator.wakes = append(s.groupActivator.wakes, time.Now())
	}
	s.groupActivator.mu.Unlock()

	assertGroupActivatorClosed(t, groupActivatorConn(t, listenPort))
	assertNoControlPlaneForward(t, cpAccepted)
}
