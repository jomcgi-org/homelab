package driver

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

// userHZ is the kernel clock-tick rate that /proc/<pid>/stat's utime/stime are
// counted in. It is 100 on effectively every Linux build (USER_HZ), including
// the daemon's container, so we treat it as a constant rather than paying a cgo
// sysconf(_SC_CLK_TCK) call. This makes CPU resolution ~10ms, coarse for a
// sub-20ms python run but fine for the semgrep/agent scans this is used to
// right-size.
const userHZ = 100

// Stats reads the guest's firecracker process resource counters from /proc and
// returns them as whole-invocation totals (see substrate.GuestStats). It is
// meant to be called once, just before Release kills the process: /proc/<pid>
// disappears with the process, so a later read would fail. Best-effort by
// contract; the caller records the values on a span and continues on error.
func (d *Driver) Stats(h substrate.Handle) (substrate.GuestStats, error) {
	inst := d.get(h.ID)
	if inst == nil {
		return substrate.GuestStats{}, fmt.Errorf("driver: stats of unknown handle %q", h.ID)
	}
	pid := inst.proc.Pid()
	if pid <= 0 {
		return substrate.GuestStats{}, fmt.Errorf("driver: stats: no pid for handle %q", h.ID)
	}
	return readProcStats(pid)
}

// readProcStats reads /proc/<pid>/stat and /proc/<pid>/status for one pid and
// returns the parsed CPU and peak-RSS totals.
func readProcStats(pid int) (substrate.GuestStats, error) {
	statData, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return substrate.GuestStats{}, fmt.Errorf("driver: read proc stat: %w", err)
	}
	cpuMs, err := parseProcStatCPUMillis(statData)
	if err != nil {
		return substrate.GuestStats{}, err
	}
	statusData, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return substrate.GuestStats{}, fmt.Errorf("driver: read proc status: %w", err)
	}
	rssMib, err := parseProcStatusPeakRSSMib(statusData)
	if err != nil {
		return substrate.GuestStats{}, err
	}
	return substrate.GuestStats{CPUMillis: cpuMs, PeakRSSMib: rssMib}, nil
}

// parseProcStatCPUMillis extracts utime+stime (thread-group totals, so summed
// across every vCPU + VMM thread) from a /proc/<pid>/stat line and converts
// clock ticks to milliseconds.
//
// The comm field (field 2) is wrapped in parens and may itself contain spaces
// or parens, so we split AFTER the last ')': the remaining fields start at
// field 3 (state), making utime (field 14) index 11 and stime (field 15) index
// 12 of the remainder.
func parseProcStatCPUMillis(data []byte) (int64, error) {
	s := string(data)
	rparen := strings.LastIndexByte(s, ')')
	if rparen < 0 || rparen+2 > len(s) {
		return 0, fmt.Errorf("driver: malformed proc stat: no comm field")
	}
	fields := strings.Fields(s[rparen+1:])
	// Need up to field 15 (stime) => index 12 of the post-comm fields.
	if len(fields) < 13 {
		return 0, fmt.Errorf("driver: malformed proc stat: only %d fields after comm", len(fields))
	}
	utime, err := strconv.ParseInt(fields[11], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("driver: parse utime: %w", err)
	}
	stime, err := strconv.ParseInt(fields[12], 10, 64)
	if err != nil {
		return 0, fmt.Errorf("driver: parse stime: %w", err)
	}
	return (utime + stime) * 1000 / userHZ, nil
}

// parseProcStatusPeakRSSMib extracts the VmHWM (peak resident set high-water
// mark) line from /proc/<pid>/status, reported in kB, and returns it in MiB
// (rounded down). VmHWM is kernel-maintained, so this needs no sampling loop.
func parseProcStatusPeakRSSMib(data []byte) (int64, error) {
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "VmHWM:") {
			continue
		}
		fields := strings.Fields(line)
		// "VmHWM:" <number> "kB"
		if len(fields) < 2 {
			return 0, fmt.Errorf("driver: malformed VmHWM line: %q", line)
		}
		kb, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			return 0, fmt.Errorf("driver: parse VmHWM: %w", err)
		}
		return kb / 1024, nil
	}
	return 0, fmt.Errorf("driver: no VmHWM line in proc status")
}
