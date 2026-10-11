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
	"sync/atomic"
	"time"
)

const (
	// VMMMemoryOverheadMib covers Firecracker, virtio queues, page tables, and
	// other host-side memory which is not part of the configured guest RAM.
	VMMMemoryOverheadMib = 192
	vmUIDBase            = 20000
	vmUIDCount           = 40000
	// vmUIDBlockSize is the slice of the node-wide uid range one daemon leases:
	// 40 daemons per node, each with 1000 concurrently live VMs.
	vmUIDBlockSize = 1000
	// JailUIDBlocks is how many uid blocks the node's range holds, the bound
	// ExecLauncher.UIDBlockBase must stay under.
	JailUIDBlocks = vmUIDCount / vmUIDBlockSize
	// defaultJailerBin is the jailer baked beside Firecracker in the noded image.
	defaultJailerBin = "/opt/fc/jailer"
)

// ErrJailerContainment marks a jailed launch the launcher refused because it
// could not contain the guest: no unique jail uid, no jail root, no VM cgroup,
// no PID namespace, a VMM that did not join its cgroup, or a legacy bundle with
// no jail resource metadata to stage. An armed jailer never falls back to
// running the guest unjailed.
var ErrJailerContainment = errors.New("driver: jailer containment unavailable")

// errVMMExitedBeforeSocket reports a VMM (or its jailer) that exited before its
// API socket accepted a connection.
var errVMMExitedBeforeSocket = errors.New("driver: firecracker exited before its API socket was ready")

// LaunchSpec contains the per-VM facts which must be applied before Firecracker
// can accept API configuration.
type LaunchSpec struct {
	VMID       string
	SocketPath string
	Workload   string
	Phase      string
	MemMib     int
	Resources  []JailResource
	// DirectExec marks a legacy bundle whose metadata names no rootfs, so it
	// can only run without the jailer. A disarmed launcher execs it directly,
	// as it does every guest; an armed launcher refuses it.
	DirectExec bool
	// SerialPath and OnExit are host-owned observation facts, never guest identity.
	SerialPath string
	OnExit     func(ExitReason)
}

// ExecLauncher launches real Firecracker processes. It is the production
// Launcher; tests inject a fake. fc-agentd owns FC process supervision (crash
// cleanup, orphan reaping), mirroring E2B's patterns.
type ExecLauncher struct {
	// Logger receives line-delimited Firecracker output. Guest UART output is
	// routed separately from the bounded serial sink by Driver. It defaults to
	// slog.Default when unset.
	Logger *slog.Logger
	// Bin is the firecracker binary (/opt/fc/firecracker in the noded image).
	Bin string
	// JailerBin is the matching jailer binary. It defaults to /opt/fc/jailer.
	JailerBin string
	// JailerEnabled selects the production jailer path. False is the direct-exec
	// escape hatch and preserves the previous launch behavior.
	JailerEnabled bool
	// DaemonReserveMib is the brick memory kept back for noded itself. The shared
	// VM cgroup parent is capped at the brick's memory.max minus this reserve, so
	// guests in aggregate OOM inside their own parent, never the container.
	DaemonReserveMib int
	// UIDLeaseDir is the directory holding the jail uid block locks (the
	// release's snapshot root, shared by every brick of that release on the
	// node). Jailed launches fail closed without it.
	UIDLeaseDir string
	// UIDBlockBase is the first uid block this daemon may lease. Releases that
	// share a node lease under different snapshot roots, so each one needs its
	// own disjoint range of blocks (production 0, embervm-dev 20).
	UIDBlockBase int
	// ReadyTimeout bounds how long Launch waits for the API socket to accept
	// connections before giving up.
	ReadyTimeout time.Duration
	// OOMScoreAdj, when > 0, is written to the launched Firecracker process's
	// /proc/<pid>/oom_score_adj so the guest is the kernel's first OOM victim
	// under ancestor memory pressure, never the daemon. The jailed path also has
	// its own hard memory.max. Best-effort: a write failure is logged, not fatal.
	OOMScoreAdj int
	// MountNamespace keeps direct-exec guests in the private mount namespace the
	// retired canonical-vsock bind used. It preserves that containment without
	// requiring a bind mount or self-reexec trampoline.
	MountNamespace bool
	// Env is the environment handed to the firecracker or jailer process. nil
	// selects firecrackerEnv(): PATH only. The daemon's own environment carries
	// the fleet bearer token, the store credentials and the restore capability
	// key; the VMM needs none of them, and a guest that escapes lands in exactly
	// this process, so inheriting os.Environ would hand it every secret the
	// brick holds. Tests that drive Launch through a wrapper script pass
	// os.Environ explicitly.
	Env []string
	// cgroups is an injected cgroup hierarchy for tests. Production uses the
	// process-wide manager so all launches share one delegated parent.
	cgroups *cgroupManager
	// createCgroup is the narrow launch seam used by lifecycle tests.
	createCgroup func(vmID string, limitBytes int64) (*vmCgroup, error)
	// allocateJailUID is the narrow uid-allocation seam used by lifecycle tests.
	allocateJailUID func() (int, func(), error)
	// jailerPIDNamespace applies the jailer's PID namespace to its command. nil
	// selects setNewPIDNS; unprivileged tests substitute a no-op because
	// CLONE_NEWPID needs CAP_SYS_ADMIN.
	jailerPIDNamespace func(*exec.Cmd)
	// launchModes overrides the daemon-wide launch-mode counters for tests.
	launchModes *launchModeCounters
}

// launchModeCounters are cumulative launch outcomes since daemon start, keyed
// by mode only, never by VM or workload. jailed and directExec count launches
// that returned a running VMM; refused counts every launch an armed launcher
// failed or would not contain, a legacy bundle without jail metadata included,
// since none of them falls back to running the guest unjailed. directExec
// therefore moves only on a disarmed brick.
type launchModeCounters struct {
	jailed     atomic.Uint64
	directExec atomic.Uint64
	refused    atomic.Uint64
}

// productionLaunchModes is shared by every ExecLauncher in the daemon, like the
// uid pool: the restore driver and each per-build driver hold their own
// launcher, and NodeStatus reports the daemon's total.
var productionLaunchModes launchModeCounters

func (l *ExecLauncher) modeCounters() *launchModeCounters {
	if l.launchModes != nil {
		return l.launchModes
	}
	return &productionLaunchModes
}

// firecrackerEnv is the environment a launched VMM receives when the launcher
// sets none: a fixed PATH and nothing else. os/exec would otherwise hand the
// child the daemon's full environment, which is where every brick secret lives.
func firecrackerEnv() []string {
	return []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
}

// processEnv returns the environment for a launch: the configured Env, or the
// minimal default. A copy is returned so a caller can never mutate the default.
func (l *ExecLauncher) processEnv() []string {
	if l.Env == nil {
		return firecrackerEnv()
	}
	return append([]string(nil), l.Env...)
}

var productionLaunchState = struct {
	sync.Mutex
	nextUID  int
	usedUIDs map[int]bool
	// uidBlock is this daemon's block of the node's uid range, leased on
	// first use and held for the process lifetime.
	uidBlock *uidBlockLease
	cgroups  *cgroupManager
}{usedUIDs: make(map[int]bool), cgroups: newCgroupManager()}

// PrepareJailer proves at daemon start, before anything forks, that this
// process can contain guests: the jailer binary is an executable regular file
// and the cgroup v2 memory delegation below the brick's container cgroup is in
// place. A brick that fails it must not start; noded.jailer.enabled=false is the
// escape hatch, never a silent unjailed launch.
func PrepareJailer(jailerBin string) error {
	if jailerBin == "" {
		jailerBin = defaultJailerBin
	}
	info, err := os.Stat(jailerBin)
	if err != nil {
		return fmt.Errorf("%w: jailer binary: %w", ErrJailerContainment, err)
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return fmt.Errorf("%w: jailer binary %q is not an executable regular file (mode %s)", ErrJailerContainment, jailerBin, info.Mode())
	}
	if err := productionLaunchState.cgroups.prepare(); err != nil {
		return fmt.Errorf("%w: delegate VM cgroups: %w", ErrJailerContainment, err)
	}
	return nil
}

type execProcess struct {
	cmd             *exec.Cmd
	vmID            string
	cgroup          exitCgroup
	jail            *Jail
	releaseID       func()
	stdout          *tagWriter
	stderr          *tagWriter
	waitOnce        sync.Once
	waitErr         error
	flushOnce       sync.Once
	cleanOnce       sync.Once
	logger          *slog.Logger
	workload        string
	serialPath      string
	onExit          func(ExitReason)
	exitMu          sync.Mutex
	exited          bool
	hostRequested   bool
	processEvidence string
	// exitCh, when set, is closed once wait has collected the exit status, so a
	// launch still waiting for the API socket learns of an early exit at once.
	exitCh chan struct{}
	// reaped is set, under exitMu, once the process is waited and its jail,
	// cgroup and uid are released. A VMM that exited on its own then stops
	// counting toward the driver's live projections before Release runs.
	reaped bool
	// Narrow process seams let lifecycle tests avoid real VMMs and cgroups.
	killProcess  func() error
	waitProcess  func() error
	checkRunning func() (bool, error)
}

func (p *execProcess) Kill() error {
	if p.killProcess == nil && p.cmd.Process == nil {
		return nil
	}
	p.exitMu.Lock()
	running := false
	p.processEvidence = "already_exited"
	if !p.exited {
		check := p.checkRunning
		if check == nil {
			check = func() (bool, error) { return processRunning(p.cmd.Process.Pid) }
		}
		var err error
		running, err = check()
		if err != nil {
			running = false
			p.processEvidence = "read_error"
		} else if running {
			p.processEvidence = "running"
		}
	}
	kill := p.killProcess
	if kill == nil {
		kill = p.cmd.Process.Kill
	}
	killErr := kill()
	if errors.Is(killErr, os.ErrProcessDone) {
		p.processEvidence = "already_exited"
	} else if killErr != nil {
		p.processEvidence = "kill_error"
	}
	if killErr == nil && running {
		p.hostRequested = true
	}
	p.exitMu.Unlock()
	if killErr != nil && !errors.Is(killErr, os.ErrProcessDone) {
		return killErr
	}
	// Always reap the child, even when Kill reports it already exited (a crashed
	// or panicked VM): without a Wait the dead process lingers as a zombie. Wait
	// is the sole reaper, so it is safe to call once here. A successful Kill alone
	// is not evidence of a running child: Linux also accepts it for a zombie.
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
	p.waitOnce.Do(func() {
		wait := p.waitProcess
		if wait == nil {
			wait = p.cmd.Wait
		}
		p.waitErr = wait()
		p.exitMu.Lock()
		p.exited = waitProvesExit(p.waitErr) || (p.cmd != nil && p.cmd.ProcessState != nil)
		p.exitMu.Unlock()
		p.flushOnce.Do(func() {
			if p.stdout != nil {
				p.stdout.Flush()
			}
			if p.stderr != nil {
				p.stderr.Flush()
			}
		})
		if p.exitCh != nil && p.exited {
			close(p.exitCh)
		}
	})
	return p.waitErr
}

func (p *execProcess) cleanup() {
	p.exitMu.Lock()
	exited := p.exited
	p.exitMu.Unlock()
	if !exited {
		return
	}
	p.cleanOnce.Do(func() {
		p.observeExit()
		if p.jail != nil {
			if err := p.jail.Cleanup(); err != nil {
				slog.Warn("driver: remove firecracker jail", "vm", p.vmID, "jail", p.jail.Dir, "err", err)
			}
		}
		if p.cgroup != nil {
			if err := p.cgroup.Remove(); err != nil {
				slog.Warn("driver: remove firecracker cgroup",
					"vm", p.vmID, "cgroup", p.cgroup.Path(), "err", err)
			}
		}
		if p.releaseID != nil {
			p.releaseID()
		}
		p.exitMu.Lock()
		p.reaped = true
		p.exitMu.Unlock()
	})
}

// Exited reports whether the VMM has exited and been fully reaped: waited, with
// its jail, cgroup and uid released. Until then its memory stays charged, which
// is the same rule Release follows.
func (p *execProcess) Exited() bool {
	p.exitMu.Lock()
	defer p.exitMu.Unlock()
	return p.reaped
}

// exitCause describes why the process exited, for a launch that saw it die
// before its API socket was ready. Call only after wait has returned.
func (p *execProcess) exitCause() string {
	if p.waitErr != nil {
		return p.waitErr.Error()
	}
	if p.cmd != nil && p.cmd.ProcessState != nil {
		return p.cmd.ProcessState.String()
	}
	return "exit status 0"
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

func (p *execProcess) SetGuestPhase(phase string) {
	p.stdout.SetPhase(phase)
	p.stderr.SetPhase(phase)
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
	if spec.Workload == "" {
		return nil, fmt.Errorf("driver: launch requires workload")
	}
	if spec.Phase != guestPhaseInit && spec.Phase != guestPhaseVM {
		return nil, fmt.Errorf("driver: launch phase must be %q or %q", guestPhaseInit, guestPhaseVM)
	}
	counters := l.modeCounters()
	if l.JailerEnabled && spec.DirectExec {
		// A legacy bundle names no rootfs the jail could stage, so it could only
		// run direct: root, no chroot, no VM cgroup. An armed brick refuses it
		// like any other launch it cannot contain, and never execs it.
		err := fmt.Errorf("%w: legacy bundle without jail resource metadata", ErrJailerContainment)
		counters.refused.Add(1)
		slog.Error("driver: refusing jailed launch", "vm", spec.VMID, "workload", spec.Workload, "err", err)
		return nil, err
	}
	jailed := l.JailerEnabled
	proc, err := l.launch(ctx, spec, jailed)
	switch {
	case err != nil && jailed:
		counters.refused.Add(1)
		if errors.Is(err, ErrJailerContainment) {
			// Every refusal is logged: each one is a guest that did not run.
			slog.Error("driver: refusing jailed launch", "vm", spec.VMID, "workload", spec.Workload, "err", err)
		}
	case err != nil:
	case jailed:
		counters.jailed.Add(1)
	default:
		counters.directExec.Add(1)
	}
	return proc, err
}

func (l *ExecLauncher) launch(ctx context.Context, spec LaunchSpec, jailed bool) (Process, error) {
	_ = os.Remove(spec.SocketPath)

	var (
		cmd       *exec.Cmd
		jail      *Jail
		cg        *vmCgroup
		releaseID func()
	)
	if jailed {
		allocateUID := l.allocateUID
		if l.allocateJailUID != nil {
			allocateUID = l.allocateJailUID
		}
		uid, release, err := allocateUID()
		if err != nil {
			return nil, fmt.Errorf("%w: allocate jail uid: %w", ErrJailerContainment, err)
		}
		gid := jailResourceGID(spec.Resources, uid)
		releaseID = release
		jail, err = prepareJail(filepath.Dir(spec.SocketPath), l.Bin, spec.VMID, uid, gid, spec.SocketPath)
		if err != nil {
			releaseID()
			return nil, fmt.Errorf("%w: prepare jail: %w", ErrJailerContainment, err)
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
		createCgroup := func(vmID string, limitBytes int64) (*vmCgroup, error) {
			return cgroups.Create(vmID, limitBytes, l.DaemonReserveMib)
		}
		if l.createCgroup != nil {
			createCgroup = l.createCgroup
		}
		cg, err = createCgroup(spec.VMID, memoryLimitBytes(spec.MemMib))
		if err != nil {
			// Fail closed: a guest that cannot get its own memory ceiling is
			// refused, never run directly as root in the daemon's cgroup.
			_ = jail.Cleanup()
			_ = os.Remove(spec.SocketPath)
			releaseID()
			return nil, fmt.Errorf("%w: create VM cgroup: %w", ErrJailerContainment, err)
		}
		cmd = exec.Command(l.jailerBin(), buildJailerArgs(l.Bin, spec.VMID, uid, gid, jail.BaseDir, cg.ParentArg(), jail.APISocketPath())...)
		newPIDNS := l.jailerPIDNamespace
		if newPIDNS == nil {
			newPIDNS = setNewPIDNS
		}
		newPIDNS(cmd)
	} else {
		cmd = exec.Command(l.Bin, buildDirectArgs(spec.VMID, spec.SocketPath)...)
		if l.MountNamespace {
			setUnshareMountNS(cmd)
		}
	}
	cmd.Env = l.processEnv()
	// Firecracker's own inherited stdout/stderr are line-delimited into the
	// daemon's structured log. Guest UART bytes never use these streams: the
	// driver follows the separately rate-limited serial sink (issue #4404).
	logger := l.Logger
	if logger == nil {
		logger = slog.Default()
	}
	stdout := newTagWriter(logger.With("stream", "stdout"), "firecracker", spec.Workload, spec.Phase)
	stderr := newTagWriter(logger.With("stream", "stderr"), "firecracker", spec.Workload, spec.Phase)
	cmd.Stdout = stdout
	cmd.Stderr = stderr
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
		if jailed {
			// The jailer, in its own PID namespace, is the containment. A spawn
			// failure (including EPERM on CLONE_NEWPID) is never retried without it.
			return nil, fmt.Errorf("%w: start jailer: %w", ErrJailerContainment, err)
		}
		return nil, fmt.Errorf("driver: start firecracker: %w", err)
	}
	proc := &execProcess{
		cmd: cmd, vmID: spec.VMID, jail: jail, releaseID: releaseID,
		stdout: stdout, stderr: stderr,
		logger: l.Logger, workload: spec.Workload, serialPath: spec.SerialPath, onExit: spec.OnExit,
		exitCh: make(chan struct{}),
	}
	if cg != nil {
		proc.cgroup = cg
	}
	// Observe unexpected death immediately, not only when Release eventually
	// runs. Wait is cached and remains the sole reaper. Once a VMM that exited on
	// its own is reaped it stops counting toward the driver's live and claimed
	// projections, but this watcher changes no ownership or Release semantics:
	// the live entry stays until Release.
	go func() { _ = proc.Wait() }()

	if l.OOMScoreAdj > 0 {
		if err := setOOMScoreAdj(cmd.Process.Pid, l.OOMScoreAdj); err != nil {
			// Best-effort hardening; the cap still bounds memory if this fails.
			slog.Warn("driver: set firecracker oom_score_adj",
				"vm", spec.VMID, "pid", cmd.Process.Pid, "score", l.OOMScoreAdj, "err", err)
		}
	}

	fail := func(cause error) (Process, error) {
		partial, err := failedProcessLaunch(proc, cause)
		if partial == nil {
			_ = os.Remove(spec.SocketPath)
		}
		return partial, err
	}
	timeout := l.ReadyTimeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	if err := waitForSocketOrExit(ctx, spec.SocketPath, timeout, proc.exitCh); err != nil {
		if errors.Is(err, errVMMExitedBeforeSocket) {
			err = fmt.Errorf("%w (API socket %q): %s", errVMMExitedBeforeSocket, spec.SocketPath, proc.exitCause())
			if jailed {
				// The jailer exits before Firecracker binds its socket when it
				// cannot build the jail: chroot, device nodes, cgroup join, uid drop.
				err = fmt.Errorf("%w: %w", ErrJailerContainment, err)
			}
		}
		return fail(err)
	}
	if cg != nil {
		// The jailer joins its leaf before it execs Firecracker, and Firecracker
		// binds the API socket only after that exec, so a ready socket means the
		// join has already happened. The jailer skips the join silently when the
		// leaf is missing, so membership is checked, not assumed.
		contained, err := cg.contains(cmd.Process.Pid)
		if err != nil || !contained {
			if err == nil {
				err = fmt.Errorf("firecracker pid %d is not in VM cgroup %q", cmd.Process.Pid, cg.Path())
			}
			return fail(fmt.Errorf("%w: verify VM cgroup membership: %w", ErrJailerContainment, err))
		}
	}
	return proc, nil
}

func (l *ExecLauncher) jailerBin() string {
	if l.JailerBin != "" {
		return l.JailerBin
	}
	return defaultJailerBin
}

// allocateUID hands out a jail uid from this daemon's leased block. The block
// is leased on first use and kept for the process lifetime, so uids stay unique
// across every brick of this release on the node, and across releases as long
// as each leases from its own disjoint block range (UIDBlockBase).
func (l *ExecLauncher) allocateUID() (int, func(), error) {
	productionLaunchState.Lock()
	defer productionLaunchState.Unlock()
	if productionLaunchState.uidBlock == nil {
		lease, err := leaseUIDBlock(l.UIDLeaseDir, l.UIDBlockBase)
		if err != nil {
			return 0, nil, err
		}
		productionLaunchState.uidBlock = lease
		productionLaunchState.nextUID = 0
	}
	base := productionLaunchState.uidBlock.base
	for range vmUIDBlockSize {
		uid := base + productionLaunchState.nextUID%vmUIDBlockSize
		productionLaunchState.nextUID = (productionLaunchState.nextUID + 1) % vmUIDBlockSize
		if !productionLaunchState.usedUIDs[uid] {
			productionLaunchState.usedUIDs[uid] = true
			return uid, func() {
				productionLaunchState.Lock()
				delete(productionLaunchState.usedUIDs, uid)
				productionLaunchState.Unlock()
			}, nil
		}
	}
	return 0, nil, fmt.Errorf("driver: jailer uid block %d-%d exhausted (%d concurrent VMs)", base, base+vmUIDBlockSize-1, vmUIDBlockSize)
}

func memoryLimitBytes(guestMemMib int) int64 {
	return int64(guestMemMib+VMMMemoryOverheadMib) * 1024 * 1024
}

func buildDirectArgs(vmID, socketPath string) []string {
	return []string{"--api-sock", socketPath, "--id", vmID}
}

// buildJailerArgs never passes --new-pid-ns or --daemonize: noded creates the
// PID namespace itself (setNewPIDNS) so the jailer execs Firecracker in place
// and the launched process remains the VMM.
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
	return waitForSocketOrExit(ctx, socketPath, timeout, nil)
}

// waitForSocketOrExit is waitForSocket that also gives up as soon as exited is
// closed: a VMM or jailer that died before binding its socket never will, so
// waiting out the full timeout only delays the failure. A nil channel never
// fires.
func waitForSocketOrExit(ctx context.Context, socketPath string, timeout time.Duration, exited <-chan struct{}) error {
	waitCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	dialer := net.Dialer{Timeout: 200 * time.Millisecond}
	for waitCtx.Err() == nil {
		conn, err := dialer.DialContext(waitCtx, "unix", socketPath)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		// Socket startup can finish within a few milliseconds. A short retry
		// avoids imposing a 20 ms floor while still yielding between attempts.
		timer := time.NewTimer(time.Millisecond)
		select {
		case <-waitCtx.Done():
			timer.Stop()
		case <-exited:
			timer.Stop()
			return errVMMExitedBeforeSocket
		case <-timer.C:
		}
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return fmt.Errorf("driver: firecracker API socket %q not ready after %s: %w", socketPath, timeout, waitCtx.Err())
}

// A partial launch still transfers ownership unless stopping it is proven.
func failedProcessLaunch(proc Process, cause error) (Process, error) {
	if err := stopProcess(proc); err != nil {
		return proc, errors.Join(cause, err)
	}
	return nil, cause
}
