package guestagent

import (
	"encoding/json"
	"errors"
	"os"
	"testing"
)

func TestMemoryStatus(t *testing.T) {
	const mem = "MemTotal: 1024 kB\nMemAvailable: 512 kB\n"
	const psi = "some avg10=1.25 avg60=2.00 avg300=0.00 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
	for _, tc := range []struct {
		name, mem, psi, vmstat, nonce string
		readErr                       error
		wantErr, wantPSI, wantOOM     bool
	}{
		{"supported", mem, psi, "oom_kill 3\n", "opaque", nil, false, true, true},
		{"PSI absent", mem, "", "oom_kill 0\n", "opaque", nil, false, false, true},
		{"OOM absent", mem, psi, "pgfault 42\n", "opaque", nil, false, true, false},
		{"malformed meminfo", "MemTotal: nope kB\n", psi, "", "opaque", nil, true, false, false},
		{"missing available", "MemTotal: 1024 kB\n", psi, "", "opaque", nil, true, false, false},
		{"invalid available", "MemTotal: 1 kB\nMemAvailable: 2 kB\n", psi, "", "opaque", nil, true, false, false},
		{"malformed PSI", mem, "some avg10=NaN", "", "opaque", nil, true, false, false},
		{"malformed OOM", mem, psi, "oom_kill nope\n", "opaque", nil, true, false, false},
		{"read error", mem, psi, "", "opaque", errors.New("permission denied"), true, false, false},
		{"missing nonce", mem, psi, "", "", nil, true, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := New(&fakeClock{}, nil)
			a.readProc = func(path string) ([]byte, error) {
				if tc.readErr != nil {
					return nil, tc.readErr
				}
				switch path {
				case "/proc/meminfo":
					return []byte(tc.mem), nil
				case "/proc/pressure/memory":
					if tc.psi == "" {
						return nil, os.ErrNotExist
					}
					return []byte(tc.psi), nil
				case "/proc/vmstat":
					return []byte(tc.vmstat), nil
				default:
					t.Fatalf("unexpected path %s", path)
					return nil, nil
				}
			}
			b, _ := json.Marshal(request{Cmd: "memory_status", Nonce: tc.nonce})
			r := a.handle(b)
			if (r.Err != "") != tc.wantErr {
				t.Fatalf("response: %+v", r)
			}
			if tc.wantErr {
				if r.MemoryStatus != nil {
					t.Fatal("error returned data")
				}
				return
			}
			m := r.MemoryStatus
			if m == nil || m.Nonce != tc.nonce || *m.TotalBytes != 1024*1024 || *m.AvailableBytes != 512*1024 || *m.PSISupported != tc.wantPSI || *m.OOMSupported != tc.wantOOM {
				t.Fatalf("status: %+v", m)
			}
			out, _ := json.Marshal(r)
			if len(out) > 1024 {
				t.Fatalf("unbounded response: %d", len(out))
			}
		})
	}
}

func TestSyncClockWireUnchanged(t *testing.T) {
	a := New(&fakeClock{readBack: 42}, nil)
	a.readProc = func(string) ([]byte, error) { t.Fatal("sync_clock read proc"); return nil, nil }
	out, _ := json.Marshal(a.handle([]byte(`{"cmd":"sync_clock","epoch_ns":42}`)))
	if string(out) != `{"clock_realtime_ns":42}` {
		t.Fatalf("changed response: %s", out)
	}
	out, _ = json.Marshal(a.handle([]byte(`{"cmd":"unknown"}`)))
	if string(out) != `{"clock_realtime_ns":0,"err":"unknown command \"unknown\""}` {
		t.Fatalf("changed unknown command: %s", out)
	}
}
