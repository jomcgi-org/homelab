//go:build linux

package guestvolume

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"testing"
)

type exitCodeOnly struct{}

func (exitCodeOnly) Error() string { return "exit-code-like probe error" }
func (exitCodeOnly) ExitCode() int { return 2 }

func TestProbe(t *testing.T) {
	// Only create exit statuses here. No real volume command is executed.
	exit2 := exec.Command("sh", "-c", "exit 2").Run()
	exit4 := exec.Command("sh", "-c", "exit 4").Run()
	for _, err := range []error{exit2, exit4} {
		if _, ok := err.(*exec.ExitError); !ok {
			t.Fatalf("expected concrete ExitError, got %v", err)
		}
	}
	for _, tc := range []struct {
		name, output  string
		err           error
		blank, failed bool
	}{
		{name: "ext4", output: "ext4\n"},
		{name: "other signature", output: "xfs"},
		{name: "empty", blank: true},
		{name: "LF", output: "\n", blank: true},
		{name: "CRLF", output: "\r\n"},
		{name: "two LFs", output: "\n\n"},
		{name: "exit 2", err: exit2, blank: true},
		{name: "exit 2 with stderr", output: "diagnostic", err: exit2, blank: true},
		{name: "exit 4", output: "stderr", err: exit4, failed: true},
		{name: "wrapped exit 2", output: "stderr", err: fmt.Errorf("wrapped: %w", exit2), failed: true},
		{name: "exit code interface is insufficient", err: exitCodeOnly{}, failed: true},
		{name: "missing executable", err: &exec.Error{Name: "blkid", Err: exec.ErrNotFound}, failed: true},
		{name: "other error", output: "stderr", err: errors.New("probe failed"), failed: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			blank, err := deviceIsBlank("/fake/device", func(name string, args ...string) ([]byte, error) {
				if name != "blkid" || !reflect.DeepEqual(args, []string{"-o", "value", "-s", "TYPE", "/fake/device"}) {
					t.Fatalf("command: %s %q", name, args)
				}
				return []byte(tc.output), tc.err
			})
			if blank != tc.blank || (err != nil) != tc.failed {
				t.Fatalf("got (%v, %v), want blank=%v failed=%v", blank, err, tc.blank, tc.failed)
			}
			if tc.failed && (!errors.Is(err, tc.err) || err.Error() != fmt.Sprintf("blkid /fake/device: %v: %s", tc.err, tc.output)) {
				t.Fatalf("error context/output lost: %v", err)
			}
		})
	}
}

func TestMountSequence(t *testing.T) {
	for _, failedStep := range []string{"", "probe", "mkfs", "mkdir", "mount"} {
		t.Run("failure="+failedStep, func(t *testing.T) {
			t.Parallel()
			failure := errors.New("injected failure")
			var events []string
			step := func(name string) error {
				events = append(events, name)
				if name == failedStep {
					return failure
				}
				return nil
			}
			ops := Operations{
				CombinedOutput: func(name string, args ...string) ([]byte, error) {
					if name == "blkid" {
						return []byte(""), step("probe")
					}
					if name != "mkfs.ext4" || !reflect.DeepEqual(args, []string{"-q", "-E", "caller-options", "/fake/device"}) {
						t.Fatalf("command %s %q", name, args)
					}
					return []byte("format stderr"), step("mkfs")
				},
				MkdirAll: func(path string, mode os.FileMode) error {
					if path != "/fake/mount" || mode != 0o755 {
						t.Fatalf("mkdir(%q, %o)", path, mode)
					}
					return step("mkdir")
				},
				Mount: func(src, dst, fs string, flags uintptr, data string) error {
					if src != "/fake/device" || dst != "/fake/mount" || fs != "ext4" || flags != 0 || data != "" {
						t.Fatalf("mount(%q, %q, %q, %d, %q)", src, dst, fs, flags, data)
					}
					return step("mount")
				},
			}
			hooks := Hooks{
				BeforeFormat: func() { events = append(events, "before") },
				AfterFormat:  func() { events = append(events, "after") },
				Mounted:      func() { events = append(events, "mounted") },
			}
			err := Mount("/fake/device", "/fake/mount", []string{"-q", "-E", "caller-options", "/fake/device"}, ops, hooks)
			want := []string{"probe", "before", "mkfs", "after", "mkdir", "mount", "mounted"}
			if failedStep != "" {
				for i, name := range want {
					if name == failedStep {
						want = want[:i+1]
						break
					}
				}
			}
			if !reflect.DeepEqual(events, want) {
				t.Fatalf("events %v, want %v", events, want)
			}
			if failedStep == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			prefix := map[string]string{"probe": "probe volume device /fake/device: blkid /fake/device:", "mkfs": "mkfs.ext4 /fake/device:", "mkdir": "mkdir volume mount path /fake/mount:", "mount": "mount /fake/device at /fake/mount:"}[failedStep]
			if !errors.Is(err, failure) || !strings.HasPrefix(err.Error(), prefix) {
				t.Fatalf("error: %v, want prefix %s and original cause", err, prefix)
			}
			if failedStep == "mkfs" && !strings.HasSuffix(err.Error(), ": format stderr") {
				t.Fatalf("missing CombinedOutput: %v", err)
			}
		})
	}
}

func TestSignatureSkipsFormatHooks(t *testing.T) {
	var events []string
	ops := Operations{
		CombinedOutput: func(name string, _ ...string) ([]byte, error) {
			events = append(events, name)
			if name != "blkid" {
				t.Fatalf("unexpected command %s", name)
			}
			return []byte("ext4\n"), nil
		},
		MkdirAll: func(string, os.FileMode) error { events = append(events, "mkdir"); return nil },
		Mount:    func(string, string, string, uintptr, string) error { events = append(events, "mount"); return nil },
	}
	err := Mount("/fake/device", "/fake/mount", nil, ops, Hooks{
		BeforeFormat: func() { t.Fatal("format hook on signature") },
		AfterFormat:  func() { t.Fatal("formatted hook on signature") },
	})
	if err != nil || !reflect.DeepEqual(events, []string{"blkid", "mkdir", "mount"}) {
		t.Fatalf("events %v, error %v", events, err)
	}
}
