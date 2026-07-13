package driver

import (
	"os"
	"runtime"
	"testing"
)

func TestParseProcStatCPUMillis(t *testing.T) {
	// utime=55, stime=12 (fields 14,15) => 67 ticks => 670ms at USER_HZ=100.
	line := []byte("1234 (firecracker) S 1 1234 1234 0 -1 4194304 100 0 0 0 55 12 0 0 20 0 5 0 100\n")
	got, err := parseProcStatCPUMillis(line)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != 670 {
		t.Errorf("cpu_ms = %d, want 670", got)
	}
}

func TestParseProcStatCPUMillisCommWithSpacesAndParens(t *testing.T) {
	// The comm field can contain spaces and parens; parsing must key on the LAST
	// ')', not the first, so utime/stime are still read correctly.
	line := []byte("1234 ((odd) fire cracker) R 1 1234 1234 0 -1 4194304 7 0 0 0 30 10 0 0 20 0 4 0 9\n")
	got, err := parseProcStatCPUMillis(line)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != 400 { // (30+10)*10
		t.Errorf("cpu_ms = %d, want 400", got)
	}
}

func TestParseProcStatCPUMillisMalformed(t *testing.T) {
	if _, err := parseProcStatCPUMillis([]byte("no parens here")); err == nil {
		t.Error("want error on a line with no comm parens, got nil")
	}
	if _, err := parseProcStatCPUMillis([]byte("1 (x) S 1 2 3")); err == nil {
		t.Error("want error when there are too few fields after comm, got nil")
	}
}

func TestParseProcStatusPeakRSSMib(t *testing.T) {
	status := []byte("Name:\tfirecracker\nState:\tS (sleeping)\nVmPeak:\t 600000 kB\nVmHWM:\t 524288 kB\nVmRSS:\t 400000 kB\n")
	got, err := parseProcStatusPeakRSSMib(status)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != 512 { // 524288 kB / 1024
		t.Errorf("peak_rss_mib = %d, want 512", got)
	}
}

func TestParseProcStatusPeakRSSMibMissing(t *testing.T) {
	if _, err := parseProcStatusPeakRSSMib([]byte("Name:\tfirecracker\nVmRSS:\t 4 kB\n")); err == nil {
		t.Error("want error when VmHWM line is absent, got nil")
	}
}

// TestReadProcStatsSelf exercises the full /proc read against this test
// process on linux, proving the file paths and parsing agree with a real
// kernel. Skipped off linux (no /proc).
func TestReadProcStatsSelf(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("proc stats only available on linux")
	}
	stats, err := readProcStats(os.Getpid())
	if err != nil {
		t.Fatalf("readProcStats(self): %v", err)
	}
	if stats.PeakRSSMib <= 0 {
		t.Errorf("peak_rss_mib = %d, want > 0 for a running process", stats.PeakRSSMib)
	}
	if stats.CPUMillis < 0 { // may be 0 at 10ms granularity for a fast test
		t.Errorf("cpu_ms = %d, want >= 0", stats.CPUMillis)
	}
}
