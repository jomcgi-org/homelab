package guestagent

import (
	"errors"
	"io"
	"math"
	"os"
	"strconv"
	"strings"
)

// ProcReader permits hermetic kernel-feature and malformed-input tests.
type ProcReader func(string) ([]byte, error)

const maxProcBytes = 256 * 1024

// MemoryStatus is an additive command on the frozen clock agent port. Pointers
// distinguish missing fields from valid zero counters when decoding a response.
// Identity is deliberately absent: only the host can attribute this evidence.
type MemoryStatus struct {
	Nonce          string    `json:"nonce"`
	TotalBytes     *uint64   `json:"total_bytes"`
	AvailableBytes *uint64   `json:"available_bytes"`
	PSISupported   *bool     `json:"psi_supported"`
	Some           *Pressure `json:"some,omitempty"`
	Full           *Pressure `json:"full,omitempty"`
	OOMSupported   *bool     `json:"oom_kill_supported"`
	OOMKill        *uint64   `json:"oom_kill,omitempty"`
}

type Pressure struct {
	Avg10 *float64 `json:"avg10"`
	Avg60 *float64 `json:"avg60"`
	Total *uint64  `json:"total"`
}

// Validate rejects incomplete and contradictory evidence, including null or
// absent support flags. Unsupported signals carry no fabricated zero values.
func (m MemoryStatus) Validate() error {
	if m.Nonce == "" || len(m.Nonce) > 128 || m.TotalBytes == nil || *m.TotalBytes == 0 ||
		m.AvailableBytes == nil || *m.AvailableBytes > *m.TotalBytes || m.PSISupported == nil || m.OOMSupported == nil {
		return errors.New("missing or invalid memory status fields")
	}
	if *m.PSISupported {
		for _, p := range []*Pressure{m.Some, m.Full} {
			if p == nil || p.Avg10 == nil || p.Avg60 == nil || p.Total == nil {
				return errors.New("incomplete PSI")
			}
			for _, avg := range []float64{*p.Avg10, *p.Avg60} {
				if math.IsNaN(avg) || math.IsInf(avg, 0) || avg < 0 || avg > 100 {
					return errors.New("invalid PSI average")
				}
			}
		}
	} else if m.Some != nil || m.Full != nil {
		return errors.New("PSI data without support")
	}
	if *m.OOMSupported != (m.OOMKill != nil) {
		return errors.New("inconsistent oom_kill support")
	}
	return nil
}

func readProcFile(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxProcBytes+1))
	if len(b) > maxProcBytes {
		return nil, errors.New("proc file too large")
	}
	return b, err
}

// ReadMemoryStatus requires usable meminfo. Only a missing PSI file or an
// absent oom_kill counter means unsupported; other read/parse failures are errors.
func ReadMemoryStatus(read ProcReader, nonce string) (MemoryStatus, error) {
	m := MemoryStatus{Nonce: nonce}
	mem, err := read("/proc/meminfo")
	if err != nil {
		return m, err
	}
	if len(mem) > maxProcBytes {
		return m, errors.New("meminfo too large")
	}
	for _, line := range strings.Split(string(mem), "\n") {
		f := strings.Fields(line)
		if len(f) == 0 || (f[0] != "MemTotal:" && f[0] != "MemAvailable:") {
			continue
		}
		if len(f) != 3 || f[2] != "kB" {
			return m, errors.New("malformed meminfo")
		}
		n, err := strconv.ParseUint(f[1], 10, 64)
		if err != nil || n > math.MaxUint64/1024 {
			return m, errors.New("invalid meminfo value")
		}
		n *= 1024
		dst := &m.TotalBytes
		if f[0] == "MemAvailable:" {
			dst = &m.AvailableBytes
		}
		if *dst != nil {
			return m, errors.New("duplicate meminfo field")
		}
		*dst = &n
	}
	psi, err := read("/proc/pressure/memory")
	supported := err == nil
	m.PSISupported = &supported
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return m, err
	}
	if supported {
		if len(psi) > maxProcBytes {
			return m, errors.New("PSI too large")
		}
		for _, line := range strings.Split(strings.TrimSpace(string(psi)), "\n") {
			f := strings.Fields(line)
			if len(f) != 5 || (f[0] != "some" && f[0] != "full") {
				return m, errors.New("malformed PSI")
			}
			p := &Pressure{}
			seen := map[string]bool{}
			for _, field := range f[1:] {
				key, value, ok := strings.Cut(field, "=")
				if !ok || seen[key] {
					return m, errors.New("malformed PSI field")
				}
				seen[key] = true
				if key == "total" {
					n, err := strconv.ParseUint(value, 10, 64)
					if err != nil {
						return m, errors.New("invalid PSI total")
					}
					p.Total = &n
				} else {
					n, err := strconv.ParseFloat(value, 64)
					if err != nil || math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || n > 100 {
						return m, errors.New("invalid PSI average")
					}
					switch key {
					case "avg10":
						p.Avg10 = &n
					case "avg60":
						p.Avg60 = &n
					case "avg300":
					default:
						return m, errors.New("unknown PSI field")
					}
				}
			}
			dst := &m.Some
			if f[0] == "full" {
				dst = &m.Full
			}
			if *dst != nil {
				return m, errors.New("duplicate PSI row")
			}
			*dst = p
		}
	}
	vmstat, err := read("/proc/vmstat")
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return m, err
	}
	if len(vmstat) > maxProcBytes {
		return m, errors.New("vmstat too large")
	}
	for _, line := range strings.Split(string(vmstat), "\n") {
		f := strings.Fields(line)
		if len(f) == 0 || f[0] != "oom_kill" {
			continue
		}
		if len(f) != 2 || m.OOMKill != nil {
			return m, errors.New("malformed oom_kill")
		}
		n, err := strconv.ParseUint(f[1], 10, 64)
		if err != nil {
			return m, errors.New("invalid oom_kill")
		}
		m.OOMKill = &n
	}
	oomSupported := m.OOMKill != nil
	m.OOMSupported = &oomSupported
	return m, m.Validate()
}
