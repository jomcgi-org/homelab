package server

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
	"github.com/jomcgi/homelab/projects/embervm/noded/guestmem"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

type memoryFetcher func(context.Context, string, string) (guestagent.MemoryStatus, error)

func (f memoryFetcher) Fetch(c context.Context, u, n string) (guestagent.MemoryStatus, error) {
	return f(c, u, n)
}

func TestGuestMemoryNodeStatusAndRelease(t *testing.T) {
	var logs bytes.Buffer
	s := New(Options{Config: config.Config{GuestMemoryFeedbackEnabled: true}, Driver: &fakeDriver{}, Logger: slog.New(slog.NewJSONHandler(&logs, nil))})
	now := time.Unix(100, 0)
	kills := uint64(3)
	s.guestMemory = guestmem.New(guestmem.Options{Enabled: true, Interval: time.Second, Now: func() time.Time { return now }, Event: s.guestMemoryEvent, Client: memoryFetcher(func(_ context.Context, uds, n string) (guestagent.MemoryStatus, error) {
		if uds != "host-uds" {
			t.Fatalf("uds=%s", uds)
		}
		total, available, yes, no := uint64(1024), uint64(0), true, false
		return guestagent.MemoryStatus{Nonce: n, TotalBytes: &total, AvailableBytes: &available, PSISupported: &no, OOMSupported: &yes, OOMKill: &kills}, nil
	})})
	h := substrate.Handle{ThreadID: "host-vm", ID: "activation-1"}
	s.trackGuestMemory(h, "host-workload", "host-uds")
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts["pending"] != 1 {
		t.Fatalf("pending=%v", ns.GuestMemoryStateCounts)
	}
	s.guestMemory.Poll(context.Background())
	kills = 5
	s.guestMemory.Poll(context.Background())
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts["ok"] != 1 || ns.GuestOomCount != 2 {
		t.Fatalf("ok=%v oom=%d", ns.GuestMemoryStateCounts, ns.GuestOomCount)
	}
	for _, field := range []string{`"event":"guest_oom"`, `"vm":"host-vm"`, `"workload":"host-workload"`, `"activation":"activation-1"`, `"delta":2`} {
		if !strings.Contains(logs.String(), field) {
			t.Fatalf("missing %s in %s", field, logs.String())
		}
	}
	now = now.Add(3 * time.Second)
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts["stale"] != 1 {
		t.Fatalf("stale=%v", ns.GuestMemoryStateCounts)
	}
	s.nodeStatus()
	if strings.Count(logs.String(), `"event":"stale"`) != 1 {
		t.Fatalf("repeated stale logs: %s", logs.String())
	}
	if err := s.reap(h, func() {}); err != nil {
		t.Fatal(err)
	}
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts["stale"] != 0 || ns.GuestOomCount != 2 {
		t.Fatalf("released=%v oom=%d", ns.GuestMemoryStateCounts, ns.GuestOomCount)
	}
	h.ID = "activation-2"
	s.trackGuestMemory(h, "host-workload", "host-uds")
	kills = 999
	s.guestMemory.Poll(context.Background())
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts["ok"] != 1 || ns.GuestOomCount != 2 {
		t.Fatalf("restore=%v oom=%d", ns.GuestMemoryStateCounts, ns.GuestOomCount)
	}
}

func TestGuestMemoryDisabledStatus(t *testing.T) {
	s := New(Options{Driver: &fakeDriver{}})
	s.trackGuestMemory(substrate.Handle{ThreadID: "vm", ID: "activation"}, "workload", "uds")
	s.StartGuestMemoryFeedback(context.Background())
	s.guestMemory.Poll(context.Background())
	if ns := s.nodeStatus(); ns.GuestMemoryStateCounts != nil || ns.GuestOomCount != 0 {
		t.Fatal("disabled telemetry published data")
	}
}
