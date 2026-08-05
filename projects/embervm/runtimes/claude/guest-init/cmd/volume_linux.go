//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// The tmpfs fallback covers the base build, which attaches no device. It also
	// backs a session until noded learns to attach a per-session drive (#4091),
	// which cannot happen while sessions are RESTORED from a shared pristine
	// snapshot: a restore never re-runs this init and never re-reads boot args, so
	// cold boot is what unlocks a per-session disk, not a drive-attach patch.
	sessionTmpfsSize = "size=2g,mode=755"
	// Where the writable device (or its tmpfs stand-in) is mounted. Both the
	// checkout and HOME are subdirectories of it, bind-mounted onto the paths the
	// image already uses, so when a real disk lands here BOTH become disk-backed
	// with no further guest change.
	defaultSessionRoot = "/session"
	// A warm base is captured without volume boot args. On the normal no-handler
	// layout, the resumed shim discovers the attached volume through this node.
	defaultVolumeDevice = "/dev/vdb"
	// The trust record's read-only master. It cannot live at its final path: a
	// bind mount over /home/runtime would hide it, so the image bakes it here and
	// this init seeds a copy into the writable HOME.
	trustRecordSeedPath = "/usr/share/ember-claude/claude.json"
)

var (
	workspaceMountPath  = "/workspace"
	runtimeHomePath     = "/home/runtime"
	mountFn             = unix.Mount
	mountVolumeDeviceFn = mountVolumeDevice
	mkdirAllFn          = os.MkdirAll
	chownFn             = os.Chown
	chmodFn             = os.Chmod
	readFileFn          = os.ReadFile
	writeFileFn         = os.WriteFile
	statFn              = os.Stat
	deviceAvailableFn   = deviceAvailable
	isMountedFn         = isMounted
	mountInfoFn         = os.ReadFile
)

// mountWorkspaceVolume gives the guest its writable state.
//
// The rootfs is attached READ-ONLY on every boot (noded RootfsReadOnly: one
// rootfs file backs every restored clone), so nothing under / can be written,
// including /home/runtime. That is not incidental to this runtime: the CLI keeps
// its config at ~/.claude.json and its session transcript at
// ~/.claude/projects/<dir>/<session-id>.jsonl, and the transcript is what
// `claude --resume` replays when the shim respawns a CLI that died mid-session.
//
// So HOME must be writable, and it must NOT stay tmpfs: tmpfs pages are
// unreclaimable, so memMib would have to be sized for the largest transcript a
// session will ever write (they reach tens of MiB) and the memfile would grow
// with the conversation. Putting HOME on the same mount as the checkout means the
// disk #4091 attaches makes both disk-backed at once.
func mountWorkspaceVolume(logger *slog.Logger) error {
	return ensureWorkspaceVolume(logger)
}

// ensureWorkspaceVolume gives the guest its writable state, whether called by
// PID 1 on a cold boot or by the surviving shim after a snapshot resume.
//
// A restored VM resumes after guest-init, so init-only mounts cannot repair a
// volume device that was attached or repointed after the snapshot. The shim
// calls this through the first real turn. The mountinfo guard is the important
// idempotency boundary: a cold guest is already mounted, while a resumed guest
// mounts the device that is present after the driver swaps the backing file.
func ensureWorkspaceVolume(logger *slog.Logger) error {
	root, dev := defaultSessionRoot, ""
	if raw, err := os.ReadFile(procCmdlinePath); err == nil {
		dev = valueFromCmdline(string(raw), volumeDevCmdlineKey)
		if mount := valueFromCmdline(string(raw), volumeMountCmdlineKey); mount != "" {
			root = mount
		}
	}
	if dev == "" && deviceAvailableFn(defaultVolumeDevice) {
		dev = defaultVolumeDevice
	}

	if isMountedFn(root) {
		logger.Info("session volume: already mounted", "mount", root)
		return nil
	}

	// The device node is the explicit availability marker. A base build has no
	// volume node when guest-init runs, so it keeps the tmpfs fallback; a
	// restored guest sees the node only after the volume has been attached and
	// repointed. This avoids treating a stale boot argument as a real disk.
	if dev != "" && deviceAvailableFn(dev) {
		// Fail closed: a guest told it has a persistent device cannot run correctly
		// without it, and silently continuing on tmpfs would lose the session.
		if err := mountVolumeDeviceFn(logger, dev, root); err != nil {
			return fmt.Errorf("mount requested volume device %s at %s: %w", dev, root, err)
		}
		logger.Info("session volume: mounted", "device", dev, "mount", root)
	} else {
		if err := mkdirAllFn(root, 0o755); err != nil {
			return fmt.Errorf("mkdir session root %s: %w", root, err)
		}
		if err := mountFn("tmpfs", root, "tmpfs", 0, sessionTmpfsSize); err != nil {
			return fmt.Errorf("mount tmpfs session root %s: %w", root, err)
		}
		logger.Info("session volume: no device attached, using tmpfs", "mount", root)
	}

	if err := bindWritable(root+"/workspace", workspaceMountPath); err != nil {
		return err
	}
	if err := bindWritable(root+"/home", runtimeHomePath); err != nil {
		return err
	}
	// The image creates /home/runtime/.claude, but the bind above hides it. Just
	// create it on the writable side; it is already reachable through the bind, so
	// it needs no mount of its own. The CLI would almost certainly mkdir it on
	// demand, but this costs two syscalls and removes a failure mode that would
	// only surface as a missing transcript after a full deploy.
	claudeDir := root + "/home/.claude"
	if err := mkdirAllFn(claudeDir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", claudeDir, err)
	}
	if err := chownFn(claudeDir, 65532, 65532); err != nil {
		return fmt.Errorf("chown %s: %w", claudeDir, err)
	}
	return seedTrustRecord(logger)
}

func deviceAvailable(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func isMounted(path string) bool {
	data, err := mountInfoFn("/proc/self/mountinfo")
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 5 && unescapeMountInfoPath(fields[4]) == path {
			return true
		}
	}
	return false
}

func unescapeMountInfoPath(path string) string {
	return strings.NewReplacer(`\040`, " ", `\011`, "\t", `\134`, `\`).Replace(path)
}

// bindWritable creates src on the writable mount, bind-mounts it over target (a
// read-only rootfs directory the image already provides), and hands it to the
// runtime uid. The chown lands on the bind SOURCE, which is writable; chowning
// the rootfs path directly is exactly what failed the base build with EROFS.
func bindWritable(src, target string) error {
	if err := mkdirAllFn(src, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", src, err)
	}
	if err := chownFn(src, 65532, 65532); err != nil {
		return fmt.Errorf("chown %s: %w", src, err)
	}
	if err := chmodFn(src, 0o755); err != nil {
		return fmt.Errorf("chmod %s: %w", src, err)
	}
	if err := mountFn(src, target, "", unix.MS_BIND, ""); err != nil {
		return fmt.Errorf("bind %s onto %s: %w", src, target, err)
	}
	return nil
}

// seedTrustRecord copies the baked ~/.claude.json into the now-writable HOME.
//
// Without it the CLI reports "Error: Workspace not trusted" and waits on a dialog
// no one is there to answer. Only when ABSENT: on a session whose disk already
// carries a record, the CLI's own writes (onboarding state) must win over the
// image's copy, or every cold boot would silently roll that state back.
func seedTrustRecord(logger *slog.Logger) error {
	dst := runtimeHomePath + "/.claude.json"
	if _, err := statFn(dst); err == nil {
		logger.Info("trust record: already present, leaving it alone", "path", dst)
		return nil
	}
	record, err := readFileFn(trustRecordSeedPath)
	if err != nil {
		return fmt.Errorf("read trust record seed %s: %w", trustRecordSeedPath, err)
	}
	if err := writeFileFn(dst, record, 0o644); err != nil {
		return fmt.Errorf("write trust record %s: %w", dst, err)
	}
	if err := chownFn(dst, 65532, 65532); err != nil {
		return fmt.Errorf("chown trust record %s: %w", dst, err)
	}
	logger.Info("trust record: seeded", "path", dst)
	return nil
}

func mountVolumeDevice(logger *slog.Logger, dev, mountPath string) error {
	blank, err := deviceIsBlank(dev)
	if err != nil {
		return fmt.Errorf("probe volume device %s: %w", dev, err)
	}
	if blank {
		// Lazy init keeps a first boot off the readiness cliff: a plain mkfs of
		// a 10 GiB volume writes every inode table and the journal up front,
		// inside the guest, ON the boot path, which blows the readiness budget
		// and the guest never serves /shim/ready (observed live: the VM boots,
		// both drives attach, then silence until the deadline). With lazy
		// itable/journal init the kernel finishes that work in the background
		// after mount, and discard skips trimming a freshly sparse file.
		start := time.Now()
		logger.Info("session volume: no filesystem signature, formatting ext4", "device", dev)
		args := []string{"-q", "-E", "lazy_itable_init=1,lazy_journal_init=1,nodiscard", dev}
		if out, err := exec.Command("mkfs.ext4", args...).CombinedOutput(); err != nil {
			return fmt.Errorf("mkfs.ext4 %s: %w: %s", dev, err, string(out))
		}
		logger.Info("session volume: formatted", "device", dev, "took", time.Since(start).String())
	}
	if err := mkdirAllFn(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir volume mount path %s: %w", mountPath, err)
	}
	if err := mountFn(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	return nil
}

func deviceIsBlank(dev string) (bool, error) {
	out, err := exec.Command("blkid", "-o", "value", "-s", "TYPE", dev).CombinedOutput()
	if err == nil {
		return len(trimTrailingNewline(out)) == 0, nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok && exitErr.ExitCode() == 2 {
		return true, nil
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}

func trimTrailingNewline(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		return b[:n-1]
	}
	return b
}
