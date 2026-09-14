// Command dev-smoke exercises the real noded driver on a local Linux KVM host.
// It is a development probe, with no untrusted input and the jailer disabled.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"github.com/jomcgi/homelab/projects/embervm/noded/vsockhttp"
)

func main() {
	driver.ExecMountTrampoline()
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() (result error) {
	root := flag.String("state", "/var/lib/embervm-dev", "local development state directory")
	kernel := flag.String("kernel", "", "ARM guest kernel")
	rootfs := flag.String("rootfs", "", "read-only probe rootfs")
	fc := flag.String("firecracker", "", "Firecracker binary")
	count := flag.Int("vms", 4, "number of simultaneously live guests")
	mem := flag.Int("mem-mib", 1536, "memory per guest")
	listen := flag.String("listen", "", "serve synthetic scans on this loopback address instead of running the probe")
	session := flag.String("session", "", "development runner session ID")
	flag.Parse()
	if os.Geteuid() != 0 || runtime.GOARCH != "arm64" {
		return errors.New("run as root inside the ARM Linux development VM")
	}
	if *count < 1 || *count > 4 || *mem < 128 || *mem > 1536 {
		return errors.New("development probe supports 1..4 guests with 128..1536 MiB each")
	}
	for _, path := range []string{*kernel, *rootfs, *fc} {
		if !filepath.IsAbs(path) {
			return fmt.Errorf("artifact path must be absolute: %q", path)
		}
	}
	if err := os.MkdirAll(*root, 0o750); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(*root, "run.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("another development probe is running")
	}
	signalCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(signalCtx, 3*time.Minute)
	defer cancel()
	self, err := os.Executable()
	if err != nil {
		return err
	}
	canonical := filepath.Join(*root, "v")
	if err := os.MkdirAll(canonical, 0o750); err != nil {
		return err
	}
	cfg := driver.Config{
		KernelImagePath: *kernel, RootfsPath: *rootfs, RootfsReadOnly: true,
		HarnessInit: "/init", VCPUs: 1, MemMib: *mem,
		KernelBootArgs: "console=ttyS0 reboot=k panic=1 pci=off keep_bootcon",
		SnapshotRoot:   *root, CanonicalVsockDir: canonical,
		Node: "apple-dev", Arch: "arm64",
	}
	d := driver.New(cfg, &driver.ExecLauncher{Bin: *fc, Self: self, VsockBindTarget: canonical}, nil)
	transport := vsockhttp.NewTransport()
	live := make(map[string]substrate.Handle)
	release := func(h substrate.Handle) error {
		if err := d.Release(context.Background(), h); err != nil {
			return err
		}
		delete(live, h.ID)
		return d.RemoveBundle(h.ThreadID)
	}
	defer func() {
		for _, h := range live {
			result = errors.Join(result, release(h))
		}
	}()
	ready := func(h substrate.Handle) error {
		readyCtx, readyCancel := context.WithTimeout(ctx, 20*time.Second)
		defer readyCancel()
		return transport.WaitReady(readyCtx, d.VsockUDSPath(h.ThreadID), "/shim/ready")
	}
	// A base is reusable only with the same artifacts, paths and machine config.
	// Rebuilding this host command alone can reuse it. Host reboots invalidate it.
	hash := sha256.New()
	_, _ = fmt.Fprintf(hash, "dev-smoke-v1:%+v", cfg)
	bootID, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return err
	}
	_, _ = hash.Write(bootID)
	for _, path := range []string{*kernel, *rootfs, *fc} {
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		_, err = io.Copy(hash, f)
		_ = f.Close()
		if err != nil {
			return err
		}
	}
	key := hex.EncodeToString(hash.Sum(nil))[:24]
	refFile := filepath.Join(*root, "bases", key, "dev-ref.json")
	var ref substrate.SnapshotRef
	if raw, err := os.ReadFile(refFile); err == nil {
		if err := json.Unmarshal(raw, &ref); err != nil {
			return err
		}
		log.Printf("reuse base %s", key)
	} else if !os.IsNotExist(err) {
		return err
	} else {
		start := time.Now()
		h, err := d.Claim(ctx, substrate.ClaimSpec{Arch: "arm64"})
		if err != nil {
			return fmt.Errorf("cold boot: %w", err)
		}
		live[h.ID] = h
		if err := ready(h); err != nil {
			return fmt.Errorf("cold readiness: %w", err)
		}
		ref, err = d.SnapshotBase(ctx, h, key)
		if err != nil {
			return fmt.Errorf("snapshot: %w", err)
		}
		if err := release(h); err != nil {
			return err
		}
		raw, err := json.Marshal(ref)
		if err != nil {
			return err
		}
		if err := os.WriteFile(refFile, raw, 0o600); err != nil {
			return err
		}
		log.Printf("cold boot + ready + snapshot: %s", time.Since(start))
	}
	if *listen != "" {
		return serveScans(signalCtx, d, transport, ref, *count, *listen, *session)
	}
	request := func(h substrate.Handle, method, value, want string) error {
		requestCtx, requestCancel := context.WithTimeout(ctx, 5*time.Second)
		defer requestCancel()
		req, err := http.NewRequestWithContext(requestCtx, method, "http://vsock/state", strings.NewReader(value))
		if err != nil {
			return err
		}
		resp, err := transport.RoundTrip(requestCtx, d.VsockUDSPath(h.ThreadID), req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(io.LimitReader(resp.Body, 8192))
		if err != nil {
			return err
		}
		if resp.StatusCode != 200 || string(body) != want {
			return fmt.Errorf("guest %s: status=%d body=%q, want %q", h.ID, resp.StatusCode, body, want)
		}
		return nil
	}
	// Keep every guest alive while marking them, then read each marker back.
	// A second wave proves that dirty memory and tmpfs never enter the base.
	for wave := 1; wave <= 2; wave++ {
		start := time.Now()
		var guests []substrate.Handle
		for i := 0; i < *count; i++ {
			h, err := d.Claim(ctx, substrate.ClaimSpec{Arch: "arm64", BaseSnapshotRef: ref})
			if err != nil {
				return fmt.Errorf("restore: %w", err)
			}
			live[h.ID] = h
			guests = append(guests, h)
			if err := ready(h); err != nil {
				return fmt.Errorf("restored readiness: %w", err)
			}
		}
		log.Printf("wave %d: %d guests restored and ready in %s", wave, d.LiveCount(), time.Since(start))
		for _, h := range guests {
			if err := request(h, http.MethodGet, "", "\n"); err != nil {
				return fmt.Errorf("fresh guest state: %w", err)
			}
			if err := request(h, http.MethodPut, h.ID, h.ID+"\n"+h.ID); err != nil {
				return err
			}
		}
		for _, h := range guests {
			if err := request(h, http.MethodGet, "", h.ID+"\n"+h.ID); err != nil {
				return fmt.Errorf("sibling isolation: %w", err)
			}
		}
		for _, h := range guests {
			if err := release(h); err != nil {
				return err
			}
		}
		if d.LiveCount() != 0 {
			return errors.New("guest processes remained after teardown")
		}
		log.Printf("wave %d: memory/tmpfs isolation and teardown passed", wave)
	}
	log.Printf("PASS: two waves of %d real Firecracker guests", *count)
	return nil
}
