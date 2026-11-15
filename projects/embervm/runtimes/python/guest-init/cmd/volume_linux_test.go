//go:build linux

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvolume"
)

func TestVolumeSequence(t *testing.T) {
	// These commands only produce concrete exit errors. All volume operations,
	// including blkid and mkfs, are fakes; /fake/volume is never opened.
	exitError := func(code string) error {
		t.Helper()
		err := exec.Command("sh", "-c", "exit "+code).Run()
		if _, ok := err.(*exec.ExitError); !ok {
			t.Fatalf("expected concrete exit error: %v", err)
		}
		return err
	}
	exit2, exit4 := exitError("2"), exitError("4")
	missing := &exec.Error{Name: "blkid", Err: exec.ErrNotFound}
	failure := errors.New("injected failure")
	for _, tc := range []struct {
		name, output      string
		probeErr          error
		blank             bool
		failAt, wantError string
	}{
		{name: "signature", output: "ext4\n"},
		{name: "empty", blank: true},
		{name: "single LF", output: "\n", blank: true},
		{name: "CRLF", output: "\r\n"},
		{name: "two LFs", output: "\n\n"},
		{name: "exit 2", probeErr: exit2, blank: true},
		{name: "exit 2 with output", output: "stderr diagnostic", probeErr: exit2, blank: true},
		{name: "exit 4", output: "stderr diagnostic", probeErr: exit4, wantError: "probe volume device /fake/volume: blkid /fake/volume:"},
		{name: "missing executable", probeErr: missing, wantError: "probe volume device /fake/volume: blkid /fake/volume:"},
		{name: "non exit error", output: "captured stderr", probeErr: failure, wantError: "probe volume device /fake/volume: blkid /fake/volume:"},
		{name: "wrapped exit 2", output: "captured stderr", probeErr: fmt.Errorf("wrapped: %w", exit2), wantError: "probe volume device /fake/volume: blkid /fake/volume:"},
		{name: "mkfs fails", blank: true, failAt: "mkfs", wantError: "mkfs.ext4 /fake/volume:"},
		{name: "mkdir fails", blank: true, failAt: "mkdir", wantError: "mkdir volume mount path /fake/mount:"},
		{name: "mount fails", blank: true, failAt: "mount", wantError: "mount /fake/volume at /fake/mount:"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var events []string
			var logs bytes.Buffer
			ops := guestvolume.Operations{
				CombinedOutput: func(name string, args ...string) ([]byte, error) {
					switch name {
					case "blkid":
						events = append(events, "probe")
						if !reflect.DeepEqual(args, []string{"-o", "value", "-s", "TYPE", "/fake/volume"}) {
							t.Fatalf("blkid args: %q", args)
						}
						return []byte(tc.output), tc.probeErr
					case "mkfs.ext4":
						events = append(events, "mkfs")
						if !reflect.DeepEqual(args, []string{"-q", "/fake/volume"}) {
							t.Fatalf("mkfs args: %q", args)
						}
						if !strings.Contains(logs.String(), "stateful volume: no filesystem signature, formatting ext4") {
							t.Fatal("format log must precede mkfs")
						}
						if tc.failAt == "mkfs" {
							return []byte("mkfs stderr"), failure
						}
						return nil, nil
					default:
						t.Fatalf("unexpected command %q", name)
						return nil, failure
					}
				},
				MkdirAll: func(path string, mode os.FileMode) error {
					events = append(events, "mkdir")
					if path != "/fake/mount" || mode != 0o755 {
						t.Fatalf("mkdir(%q, %o)", path, mode)
					}
					if tc.failAt == "mkdir" {
						return failure
					}
					return nil
				},
				Mount: func(src, dst, fs string, flags uintptr, data string) error {
					events = append(events, "mount")
					if src != "/fake/volume" || dst != "/fake/mount" || fs != "ext4" || flags != 0 || data != "" {
						t.Fatalf("mount(%q, %q, %q, %d, %q)", src, dst, fs, flags, data)
					}
					if strings.Contains(logs.String(), "stateful volume: mounted") {
						t.Fatal("mounted log before mount succeeds")
					}
					if tc.failAt == "mount" {
						return failure
					}
					return nil
				},
			}
			err := mountVolumeWithOps(slog.New(slog.NewJSONHandler(&logs, nil)), "/fake/volume", "/fake/mount", ops)
			wantEvents := []string{"probe"}
			if tc.wantError == "" || tc.failAt != "" {
				if tc.blank {
					wantEvents = append(wantEvents, "mkfs")
				}
				if tc.failAt != "mkfs" {
					wantEvents = append(wantEvents, "mkdir")
				}
				if tc.failAt != "mkfs" && tc.failAt != "mkdir" {
					wantEvents = append(wantEvents, "mount")
				}
			}
			if !reflect.DeepEqual(events, wantEvents) {
				t.Fatalf("events %v, want %v", events, wantEvents)
			}
			if tc.wantError == "" {
				if err != nil {
					t.Fatal(err)
				}
			} else {
				cause := tc.probeErr
				if cause == nil {
					cause = failure
				}
				if err == nil || !strings.HasPrefix(err.Error(), tc.wantError) || !errors.Is(err, cause) {
					t.Fatalf("error %v, want prefix %q and cause %v", err, tc.wantError, cause)
				}
				output := tc.output
				if tc.failAt == "mkfs" {
					output = "mkfs stderr"
				}
				if (tc.failAt == "" || tc.failAt == "mkfs") && !strings.HasSuffix(err.Error(), ": "+output) {
					t.Fatalf("captured output missing: %v", err)
				}
			}
			var records []map[string]any
			for _, line := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
				if line == "" {
					continue
				}
				var record map[string]any
				if err := json.Unmarshal([]byte(line), &record); err != nil {
					t.Fatal(err)
				}
				records = append(records, record)
			}
			var wantMessages []string
			if tc.blank {
				wantMessages = append(wantMessages, "stateful volume: no filesystem signature, formatting ext4")
			}
			if err == nil {
				wantMessages = append(wantMessages, "stateful volume: mounted")
			}
			if len(records) != len(wantMessages) {
				t.Fatalf("logs %s, want %v", logs.String(), wantMessages)
			}
			for i, record := range records {
				if record["msg"] != wantMessages[i] || record["device"] != "/fake/volume" {
					t.Fatalf("log: %v", record)
				}
				if record["msg"] == "stateful volume: mounted" && record["mount"] != "/fake/mount" {
					t.Fatalf("mount field: %v", record)
				}
			}
		})
	}
}
