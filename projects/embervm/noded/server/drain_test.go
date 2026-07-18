package server

import (
	"context"
	"testing"
	"time"
)

// TestSetDrainingPublishesDeadline proves SetDraining flips the draining flag,
// records the deadline, and surfaces both on NodeStatus (the fact the control
// plane reads off WatchNode to bound its force-bank pass).
func TestSetDrainingPublishesDeadline(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	if s.isDraining() {
		t.Fatal("server should not start draining")
	}
	if got := s.nodeStatus().GetDrainDeadlineUnixMs(); got != 0 {
		t.Fatalf("drain deadline should be 0 before drain, got %d", got)
	}

	deadline := time.Now().Add(120 * time.Second)
	s.SetDraining(deadline)

	if !s.isDraining() {
		t.Fatal("server should be draining after SetDraining")
	}
	ns := s.nodeStatus()
	if !ns.GetDraining() {
		t.Fatal("NodeStatus.draining should be true")
	}
	if got, want := ns.GetDrainDeadlineUnixMs(), deadline.UnixMilli(); got != want {
		t.Fatalf("NodeStatus.drain_deadline_unix_ms = %d, want %d", got, want)
	}
}

// TestWaitForManagedDrainEmpty proves the wait returns immediately with zero
// remaining when there are no managed VMs to bank.
func TestWaitForManagedDrainEmpty(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	// A generous deadline: an empty registry must return well before it.
	remaining := s.WaitForManagedDrain(context.Background(), time.Now().Add(10*time.Second))
	if remaining != 0 {
		t.Fatalf("empty drain should return 0, got %d", remaining)
	}
}

// TestWaitForManagedDrainEarlyExit proves the wait ends as soon as the control
// plane has banked (removed) the last managed VM, long before the deadline. The
// removal signals a NodeStatus change exactly as a real Bank/Stop does.
func TestWaitForManagedDrainEarlyExit(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	s.statefulVMs.add(&statefulEntry{vmID: "vm-st1", workload: "scratch-postgres"})
	if got := s.managedLiveVMCount(); got != 1 {
		t.Fatalf("managedLiveVMCount = %d, want 1", got)
	}

	// Simulate the control plane force-banking the VM shortly after drain begins.
	go func() {
		time.Sleep(20 * time.Millisecond)
		s.statefulVMs.remove("vm-st1")
		s.signalChange()
	}()

	start := time.Now()
	// A far deadline: the early exit must be driven by the empty registry, not it.
	remaining := s.WaitForManagedDrain(context.Background(), start.Add(30*time.Second))
	if remaining != 0 {
		t.Fatalf("early-exit drain should return 0, got %d", remaining)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("early exit took %s; should have returned promptly on the bank signal", elapsed)
	}
}

// TestWaitForManagedDrainDeadline proves a VM that cannot bank in the window is
// left live at the deadline (its count is returned so the caller can log it; the
// pod then reaps it under spot semantics).
func TestWaitForManagedDrainDeadline(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	s.statefulVMs.add(&statefulEntry{vmID: "vm-st1", workload: "scratch-postgres"})

	// A short deadline with the VM never removed: the wait must return it as still
	// live once the deadline passes.
	remaining := s.WaitForManagedDrain(context.Background(), time.Now().Add(80*time.Millisecond))
	if remaining != 1 {
		t.Fatalf("deadline drain should return 1 straggler, got %d", remaining)
	}
}
