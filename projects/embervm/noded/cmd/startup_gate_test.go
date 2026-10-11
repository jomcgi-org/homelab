package main

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

func TestStartupGate(t *testing.T) {
	failed := errors.New("jail cannot read rootfs")
	for _, tc := range []struct {
		name          string
		jailer        bool
		sizeClass     string
		preflightErr  error
		budget        uint64
		wantErr       string
		wantPreflight bool
	}{
		{name: "disarmed unclassed", budget: 0},
		{name: "disarmed classed with budget", sizeClass: "4gi", budget: 3840},
		{name: "disarmed classed blind to budget", sizeClass: "4gi", budget: 0, wantErr: "zero memory budget"},
		{name: "armed preflight passes", jailer: true, sizeClass: "4gi", budget: 3840, wantPreflight: true},
		{name: "armed preflight fails", jailer: true, sizeClass: "4gi", budget: 3840, preflightErr: failed, wantErr: "refusing to register", wantPreflight: true},
		{name: "armed classed blind to budget", jailer: true, sizeClass: "8gi", budget: 0, wantErr: "zero memory budget", wantPreflight: true},
		{name: "armed legacy daemonset", jailer: true, budget: 0, wantPreflight: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls []string
			var gotPaths []string
			preflight := func(_ context.Context, paths []string) error {
				calls = append(calls, "preflight")
				gotPaths = paths
				return tc.preflightErr
			}
			rootfs := func() []string { return []string{"/rootfs/a.ext4"} }
			budget := func() uint64 {
				calls = append(calls, "budget")
				return tc.budget
			}
			cfg := config.Config{JailerEnabled: tc.jailer, SizeClass: tc.sizeClass}
			err := startupGate(context.Background(), cfg, preflight, rootfs, budget)
			if tc.wantErr == "" && err != nil {
				t.Fatalf("startupGate: %v", err)
			}
			if tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("startupGate error = %v, want %q", err, tc.wantErr)
			}
			if tc.preflightErr != nil && !errors.Is(err, tc.preflightErr) {
				t.Fatalf("startupGate error = %v, want the preflight cause wrapped", err)
			}
			if ran := slices.Contains(calls, "preflight"); ran != tc.wantPreflight {
				t.Fatalf("preflight ran = %v, want %v", ran, tc.wantPreflight)
			}
			if tc.wantPreflight && !slices.Equal(gotPaths, []string{"/rootfs/a.ext4"}) {
				t.Fatalf("preflight paths = %q", gotPaths)
			}
			// The budget is read only after the preflight's launch has moved
			// noded into the jailer's daemon cgroup.
			if i, j := slices.Index(calls, "preflight"), slices.Index(calls, "budget"); i >= 0 && j >= 0 && j < i {
				t.Fatalf("budget read before the preflight: %q", calls)
			}
		})
	}
}
