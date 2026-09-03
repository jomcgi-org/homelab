package driver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

const (
	// VMMMemoryOverheadMib covers Firecracker, virtio queues, page tables, and
	// other host-side memory which is not part of the configured guest RAM.
	VMMMemoryOverheadMib = 192
	vmUIDBase            = 20000
	vmUIDCount           = 40000
)

// LaunchSpec contains the per-VM facts which must be applied before Firecracker
// can accept API configuration.
type LaunchSpec struct {
	VMID       string
	SocketPath string
	MemMib     int
	Resources  []JailResource
	DirectExec bool
}

// ExecLauncher launches real Firecracker processes. It is the production
// Launcher; tests inject a fake. fc-agentd owns FC process supervision (crash
// cleanup, orphan reaping), mirroring E2B's patterns.
type ExecLauncher struct {
	// Bin is the firecracker binary (/opt/fc/firecracker in the noded image).
	Bin string
	// JailerBin is the matching jailer binary. It defaults to /opt/fc/jailer.
	JailerBin string
	// JailerEnabled selects the production jailer path. False is the direct-exec
	// escape hatch and preserves the previous launch behavior.
	JailerEnabled bool
	// ReadyTimeout bounds how long Launch waits for the API socket to accept
	// connections before giving up.
	ReadyTimeout time.Duration
	// OOMScoreAdj, when > 0, is written to the launched Firecracker process's
	// /proc/<pid>/oom_score_adj so the guest is the kernel's first OOM victim
	// under ancestor memory pressure, never the daemon. The jailed path also has
	// its own hard memory.max. Best-effort: a write failure is logged, not fatal.
	OOMScoreAdj int
	// VsockBindTarget, when set, launches firecracker in its own mount namespace
	// with the VM's bundle dir bind-mounted over this canonical vsock dir (the path
	// the base snapshot embeds). This gives every microVM restored from one warm
	// base its own host-reachable vsock socket. Empty preserves the plain launch
	// (fc-agentd's cold-boot path). Requires Self to be set.
	VsockBindTarget string
	// Self is the path to the current executable, which must handle the "__fcmount"
	// trampoline subcommand (via ExecMountTrampoline). Required iff VsockBindTarget
	// is set.
	Self string
	// cgroups is an injected cgroup hierarchy for tests. Production uses the
	// process-wide manager so all launches share one delegated parent.
	cgroups *cgroupManager
	// createCgroup is the narrow launch seam used by lifecycle tests.
	createCgroup func(vmID string, limitBytes int64) (*vmCgroup, error)
	// allocateJailUID is the narrow uid-allocation seam used by lifecycle tests.
	allocateJailUID func() (int, func(), error)
}

var productionLaunchState = struct {
	sync.Mutex
	nextUID  int
	usedUIDs map[int]bool
	cgroups  *cgroupManager
}{usedUIDs: make(map[int]bool), cgroups: newCgroupManager()}

type execProcess struct {
	cmd       *exec.Cmd
	vmID      string
	cgroup    *vmCgroup
	jail      *Jail
	releaseID func()
	waitOnce  sync.Once
	waitErr   error
	cleanOnce sync.Once
}

func (p *execProcess) Kill() error {
	if p.cmd.Process == nil {
		return nil
	}
	killErr := p.cmd.Process.Kill()
	// Always reap the child, even when Kill reports it already exited (a crashed
	// or panicked VM): without a Wait the dead process lingers as a zombie. Wait
	// is the sole reaper, so it is safe to call once here.
	_ = p.wait()
	p.cleanup()
	if errors.Is(killErr, os.ErrProcessDone) {
		return nil
	}
	return killErr
}

func (p *execProcess) Wait() error {
	err := p.wait()
	p.cleanup()
	return err
}

func (p *execProcess) wait() error {
	p.waitOnce.Do(func() { p.waitErr = p.cmd.Wait() })
	return p.waitErr
}

func (p *execProcess) cleanup() {
	p.cleanOnce.Do(func() {
		if p.jail != nil {
			if err := p.jail.Cleanup(); err != nil {
				slog.Warn("driver: remove firecracker jail", "vm", p.vmID, "jail", p.jail.Dir, "err", err)
			}
		}
		if p.cgroup != nil {
			if killed, err := p.cgroup.OOMKilled(); err == nil && killed {
				slog.Warn("driver: firecracker exited after cgroup OOM kill",
					"vm", p.vmID, "cgroup", p.cgroup.Path())
			}
			if err := p.cgroup.Remove(); err != nil {
				slog.Warn("driver: remove firecracker cgroup",
					"vm", p.vmID, "cgroup", p.cgroup.Path(), "err", err)
			}
		}
		if p.releaseID != nil {
			p.releaseID()
		}
	})
}

// Jail returns the launch's path mapper when this process is jailed.
func (p *execProcess) Jail() *Jail { return p.jail }

// Pid returns the OS pid of the firecracker process, or 0 before Start.
func (p *execProcess) Pid() int {
	if p.cmd == nil || p.cmd.Process == nil {
		return 0
	}
	return p.cmd.Process.Pid
}

// Launch starts firecracker with its API socket at spec.SocketPath and blocks until
// the socket is connectable (or the timeout/context fires).
func (l *ExecLauncher) Launch(ctx context.Context, spec LaunchSpec) (Process, error) {
	if l.Bin == "" {
		return nil, fmt.Errorf("driver: ExecLauncher.Bin is empty")
	}
	if spec.VMID == "" || spec.SocketPath == "" {
		return nil, fmt.Errorf("driver: launch requires vm id and API socket path")
	}
	_ = os.Remove(spec.SocketPath)

	var (
		cmd       *exec.Cmd
		jail      *Jail
		cg        *vmCgroup
		releaseID func()
	)
	if l.JailerEnabled && !spec.DirectExec {
		allocateUID := l.allocateUID
		if l.allocateJailUID != nil {
			allocateUID = l.allocateJailUID
		}
		uid, release, err := allocateUID()
		if err != nil {
			return nil, err
		}
		gid := jailResourceGID(spec.Resources, uid)
		releaseID = release
		jail, err = prepareJail(filepath.Dir(spec.SocketPath), l.Bin, spec.VMID, uid, gid, spec.SocketPath)
		if err != nil {
			releaseID()
			return nil, err
		}
		for _, resource := range spec.Resources {
			if _, err := jail.stageResource(resource); err != nil {
				_ = jail.Cleanup()
				_ = os.Remove(spec.SocketPath)
				releaseID()
				return nil, fmt.Errorf("driver: stage VM resource %q: %w", resource.Role, err)
			}
		}
		cgroups := l.cgroups
		if cgroups == nil {
			cgroups = productionLaunchState.cgroups
		}
		createCgroup := cgroups.Create
		if l.createCgroup != nil {
			createCgroup = l.createCgroup
		}
		cg, err = createCgroup(spec.VMID, memoryLimitBytes(spec.MemMib))
		if err != nil {
			_ = jail.Cleanup()
			_ = os.Remove(spec.SocketPath)
			releaseID()
			jail = nil
			releaseID = nil
			if cgroups.shouldLogFailure(err) {
				slog.Error("driver: jailer disabled for launch after cgroup setup failure",
					"vm", spec.VMID, "err", err)
			}
			cmd = exec.Command(l.Bin, buildDirectArgs(spec.VMID, spec.SocketPath)...)
		} else {
			cmd = exec.Command(l.jailerBin(), buildJailerArgs(l.Bin, spec.VMID, uid, gid, jail.BaseDir, cg.ParentArg(), jail.APISocketPath())...)
		}
	} else if l.VsockBindTarget != "" {
		// Per-instance vsock isolation: re-exec our own __fcmount trampoline in a
		// fresh mount namespace, bind-mounting this VM's bundle dir (the api socket's
		// dir) over the canonical vsock dir embedded in the base snapshot, then exec
		// firecracker. See ExecMountTrampoline.
		if l.Self == "" {
			return nil, fmt.Errorf("driver: ExecLauncher.Self required when VsockBindTarget is set")
		}
		bindSrc := filepath.Dir(spec.SocketPath)
		cmd = exec.Command(l.Self, "__fcmount", bindSrc, l.VsockBindTarget, l.Bin, "--api-sock", spec.SocketPath, "--id", spec.VMID)
		setUnshareMountNS(cmd)
	} else {
		cmd = exec.Command(l.Bin, buildDirectArgs(spec.VMID, spec.SocketPath)...)
	}
	// Firecracker's own startup/error messages ride the daemon's stdio. The
	// GUEST console no longer does: the driver issues PUT /serial pre-Start and
	// pre-LoadSnapshot (issue #4404), pointing the UART at the per-VM bundle
	// file, so consoles cannot interleave into or flood the noded log.
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		if cg != nil {
			_ = cg.Remove()
		}
		if jail != nil {
			_ = jail.Cleanup()
		}
		if releaseID != nil {
			releaseID()
		}
		_ = os.Remove(spec.SocketPath)
		return nil, fmt.Errorf("driver: start firecracker: %w", err)
	}
	proc := &execProcess{cmd: cmd, vmID: spec.VMID, cgroup: cg, jail: jail, releaseID: releaseID}

	if l.OOMScoreAdj > 0 {
		if err := setOOMScoreAdj(cmd.Process.Pid, l.OOMScoreAdj); err != nil {
			// Best-effort hardening; the cap still bounds memory if this fails.
			slog.Warn("driver: set firecracker oom_score_adj",
				"vm", spec.VMID, "pid", cmd.Process.Pid, "score", l.OOMScoreAdj, "err", err)
		}
	}

	timeout := l.ReadyTimeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	if err := waitForSocket(ctx, spec.SocketPath, timeout); err != nil {
		_ = proc.Kill()
		_ = os.Remove(spec.SocketPath)
		return nil, err
	}
	return proc, nil
}

func (l *ExecLauncher) jailerBin() string {
	if l.JailerBin != "" {
		return l.JailerBin
	}
	return "/opt/fc/jailer"
}

func (l *ExecLauncher) allocateUID() (int, func(), error) {
	productionLaunchState.Lock()
	defer productionLaunchState.Unlock()
	for range vmUIDCount {
		uid := vmUIDBase + productionLaunchState.nextUID%vmUIDCount
		productionLaunchState.nextUID = (productionLaunchState.nextUID + 1) % vmUIDCount
		if !productionLaunchState.usedUIDs[uid] {
			productionLaunchState.usedUIDs[uid] = true
			return uid, func() {
				productionLaunchState.Lock()
				delete(productionLaunchState.usedUIDs, uid)
				productionLaunchState.Unlock()
			}, nil
		}
	}
	return 0, nil, fmt.Errorf("driver: jailer uid pool exhausted (%d concurrent VMs)", vmUIDCount)
}

func memoryLimitBytes(guestMemMib int) int64 {
	return int64(guestMemMib+VMMMemoryOverheadMib) * 1024 * 1024
}

func buildDirectArgs(vmID, socketPath string) []string {
	return []string{"--api-sock", socketPath, "--id", vmID}
}

func buildJailerArgs(bin, vmID string, uid, gid int, chrootBase, parentCgroup, apiSocket string) []string {
	return []string{
		"--id", vmID,
		"--exec-file", bin,
		"--uid", strconv.Itoa(uid),
		"--gid", strconv.Itoa(gid),
		"--cgroup-version", "2",
		"--parent-cgroup", parentCgroup,
		"--chroot-base-dir", chrootBase,
		"--",
		"--api-sock", apiSocket,
	}
}

// setOOMScoreAdj writes score to /proc/<pid>/oom_score_adj, biasing the kernel
// OOM killer to pick this process first. Valid range is -1000..1000; the daemon
// must have permission (fc-agentd runs privileged on node-4).
func setOOMScoreAdj(pid, score int) error {
	path := fmt.Sprintf("/proc/%d/oom_score_adj", pid)
	return os.WriteFile(path, []byte(strconv.Itoa(score)), 0o644)
}

func waitForSocket(ctx context.Context, socketPath string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		conn, err := net.DialTimeout("unix", socketPath, 200*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("driver: firecracker API socket %q not ready after %s", socketPath, timeout)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
