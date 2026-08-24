// Per-VM bounded serial output sink (issue #4404).
//
// Every Firecracker process used to inherit the daemon's stdout/stderr
// (ExecLauncher.Launch), so all guest consoles interleaved into the noded log
// and a probe VM's console was effectively unfindable during the #4389
// investigation. Firecracker v1.14+ instead accepts PUT /serial with a
// serial_out_path, pointing the microVM's UART at a host file. The driver now
// pre-creates one serial output file per VM inside the thread's bundle dir,
// points every cold boot AND every snapshot restore at it (serial config is
// deliberately not part of snapshot state), and rate limits it so a noisy
// guest cannot flood the node.
//
// Boundedness:
//   - Disk: FC appends to the file through a bandwidth token bucket (64 KiB/s
//     sustained, 1 MiB burst), so worst-case growth per running VM is capped
//     by rate x lifetime; the file is truncated at each new incarnation of the
//     same thread dir; and it is reclaimed with the bundle by RemoveBundle.
//     When the bucket empties, FC DROPS further UART bytes (the v1.16
//     uart.rate_limiter_dropped_bytes metric counts them) rather than
//     buffering or blocking the guest, which keeps recent console output
//     flowing: exactly the tail diagnostics need.
//   - Memory: nothing is buffered in the daemon while the VM runs. Only on a
//     boot/restore FAILURE does serialTail read at most serialTailBytes from
//     the end of the sink, and that slice rides the returned error (which
//     callers log and forward as gRPC status messages), so failure
//     diagnostics carry the guest's last words without any steady-state cost.
package driver

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// serialOutputName is the guest console sink file inside a thread's bundle
// dir. It sits in the bundle so it is reclaimed with RemoveBundle GC like the
// rootfs and never collides with the snapfile/memfile pair or the sidecars.
const serialOutputName = "serial.log"

const (
	// serialBandwidthBytesPerSec caps the guest's sustained UART write rate to
	// 64 KiB/s: far above any real console traffic (a classic 115200-baud UART
	// tops out around 11 KiB/s) yet well below disk bandwidth, so even a guest
	// spamming its console cannot flood the node (~5.5 GiB/day absolute worst
	// case for one long-lived VM, reclaimed with its bundle).
	serialBandwidthBytesPerSec = 64 * 1024
	// serialBandwidthRefillMs refills the token bucket every second, making
	// size/refill_time the sustained rate above.
	serialBandwidthRefillMs = 1000
	// serialBurstBytes lets a booting kernel print its usual tens of KiB (plus
	// a panic trace) without throttling before the sustained cap applies.
	serialBurstBytes = 1024 * 1024
	// serialTailBytes caps how much of the sink a failure diagnostic keeps.
	serialTailBytes = 8 * 1024
)

// serialOutputPath is the guest console sink path for a thread's bundle dir.
func (d *Driver) serialOutputPath(threadID string) string {
	return filepath.Join(d.threadDir(threadID), serialOutputName)
}

// prepareSerialOutput creates (or truncates) the thread's serial sink and
// returns its host path. Truncation bounds disk across incarnations of a
// reused thread id (orphan recovery reuses ids): only the live process's
// console matters, prior incarnations are dead. The caller has already created
// the bundle dir. 0o640 matches the other bundle files.
func (d *Driver) prepareSerialOutput(threadID string) (string, error) {
	path := d.serialOutputPath(threadID)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o640)
	if err != nil {
		return "", fmt.Errorf("driver: create serial output %q: %w", path, err)
	}
	if err := f.Close(); err != nil {
		return "", fmt.Errorf("driver: close serial output %q: %w", path, err)
	}
	return path, nil
}

// serialRateLimiter is the conservative byte rate the issue calls for: a 1 MiB
// burst for boot-time kernel spew, then 64 KiB/s sustained. Once the bucket
// empties Firecracker drops further UART bytes, so a flooding guest loses
// middle output but the most recent console lines keep flowing.
//
// n v1.16.1's PUT /serial takes the limiter as a FLAT token bucket
// ({size, refill_time, one_time_burst}), not the drive/net {bandwidth: ...}
// wrapper: the fleet answered our first wrapped body with SerdeJson "missing
// field `size`" pointing at the end of the bandwidth object.
func serialRateLimiter() *fcclient.TokenBucket {
	return &fcclient.TokenBucket{
		Size:         serialBurstBytes + serialBandwidthBytesPerSec,
		OneTimeBurst: serialBurstBytes,
		RefillTime:   serialBandwidthRefillMs,
	}
}

// issueSerial points this Firecracker process's UART at the thread's sink. It
// must run before Start on a cold boot and before LoadSnapshot on a restore
// (a restored process starts with no serial sink, and resume_vm may start the
// guest immediately). Like every other config PUT, a failure fails the boot.
func issueSerial(ctx context.Context, client fcAPI, path string) error {
	if err := client.PutSerial(ctx, fcclient.Serial{SerialOutPath: path, RateLimiter: serialRateLimiter()}); err != nil {
		return fmt.Errorf("driver: put serial: %w", err)
	}
	return nil
}

// serialTail reads at most maxBytes from the end of path, best-effort. It
// returns ok=false when the sink does not exist or cannot be read (a boot
// failure so early the sink was never created, or an unreadable file): the
// cause is reported unchanged in that case, never masked. An existing but
// empty sink returns ok=true with zero bytes (a guest that said nothing has
// nothing to say in diagnostics).
func serialTail(path string, maxBytes int64) ([]byte, bool) {
	fi, err := os.Stat(path)
	if err != nil || fi.IsDir() {
		return nil, false
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	offset := fi.Size() - maxBytes
	if offset < 0 {
		offset = 0
	}
	if _, err := f.Seek(offset, 0); err != nil {
		return nil, false
	}
	data := make([]byte, 0, min(maxBytes, fi.Size()))
	buf := make([]byte, 4096)
	for int64(len(data)) < maxBytes {
		n, err := f.Read(buf)
		if n > 0 {
			data = append(data, buf[:min(n, int(maxBytes-int64(len(data))))]...)
		}
		if err != nil {
			break // EOF or a transient read error: return what we got
		}
	}
	return data, true
}

// abortWithSerialDiag kills the half-configured Firecracker process and wraps
// cause with the guest console tail, so a failed cold boot or restore carries
// the guest's last output (kernel panic, init failure) into the error callers
// already log and forward. The wrap is best-effort and additive: with no
// readable sink (or zero bytes of output) the cause is returned unchanged.
func (d *Driver) abortWithSerialDiag(proc Process, cause error, threadID string) (substrate.Handle, error) {
	_ = proc.Kill()
	tail, ok := serialTail(d.serialOutputPath(threadID), serialTailBytes)
	if !ok || len(tail) == 0 {
		return substrate.Handle{}, cause
	}
	slog.Warn("driver: guest serial output at boot failure",
		"thread", threadID,
		"tail_bytes", len(tail),
	)
	return substrate.Handle{}, fmt.Errorf("%w\nnoded: guest serial tail (%d bytes kept):\n%s", cause, len(tail), tail)
}
