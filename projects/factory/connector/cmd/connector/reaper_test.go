package main

import (
	"testing"
	"time"
)

func TestSuspendDeletionScope(t *testing.T) {
	now := time.Now()
	p := placement{"factory-r-homelab", "homelab-codex-abcdef1234-abcde", "original"}
	old := now.Add(-6 * time.Minute)
	if !suspendExpired(p, 3, old, now) || suspendExpired(p, 2, old, now) ||
		suspendExpired(p, 3, now.Add(-5*time.Minute), now) {
		t.Fatal("incorrect suspend deadline")
	}
	for _, other := range []placement{
		{"factory", p.pod, p.uid}, {p.namespace, "other", p.uid}, {p.namespace, p.pod, ""},
	} {
		if suspendExpired(other, 3, old, now) {
			t.Fatalf("eligible for deletion: %+v", other)
		}
	}
}
