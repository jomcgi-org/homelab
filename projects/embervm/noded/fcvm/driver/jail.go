package driver

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
)

const jailResourcesName = "jail-resources.json"

// chownJailFile and chmodJailFile are the ownership seams for jail staging.
// Production uses os.Chown and os.Chmod; tests swap them to observe the
// handoffs, or to stage for a foreign jail uid without root.
var (
	chownJailFile = os.Chown
	chmodJailFile = os.Chmod
)

// JailResource records one host backing file and the path embedded in the
// Firecracker device model. Persisting this beside a snapshot lets a later jail
// stage exactly the files that snapshot will reopen.
type JailResource struct {
	Role        string `json:"role"`
	HostPath    string `json:"host_path"`
	JailPath    string `json:"jail_path"`
	Writable    bool   `json:"writable,omitempty"`
	PrivateCopy bool   `json:"private_copy,omitempty"`
}

// Jail maps noded-visible paths to the private filesystem seen by Firecracker.
type Jail struct {
	BaseDir string
	Dir     string
	RootDir string
	uid     int
	gid     int
	// ownerUID and ownerGID are the daemon's own identity. Shared inputs and
	// finished snapshot outputs belong to it, never to a recycled pool uid.
	ownerUID int
	ownerGID int

	mu        sync.Mutex
	resources map[string]JailResource
	mounts    []string
}

// jailerExecName is the chroot directory name the jailer derives from
// --exec-file. Its validate_exec_file canonicalizes the path and keeps the
// resolved file name, so a symlinked /opt/fc/firecracker pointing at
// firecracker-v1.16.1 chroots under <chroot-base>/firecracker-v1.16.1/<id>.
// A path that cannot be resolved keeps its literal name: the jailer fails on
// the same canonicalize and reports the missing binary itself.
func jailerExecName(execFile string) string {
	if resolved, err := filepath.EvalSymlinks(execFile); err == nil {
		return filepath.Base(resolved)
	}
	return filepath.Base(execFile)
}

func prepareJail(bundleDir, execFile, vmID string, uid, gid int, hostSocket string) (*Jail, error) {
	base := filepath.Join(bundleDir, "jailer")
	root := filepath.Join(base, jailerExecName(execFile), vmID, "root")
	ready := false
	defer func() {
		if !ready {
			_ = os.Remove(hostSocket)
			_ = os.RemoveAll(filepath.Dir(root))
		}
	}()
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, fmt.Errorf("driver: create jail root: %w", err)
	}
	j := &Jail{
		BaseDir: base, Dir: filepath.Dir(root), RootDir: root, uid: uid, gid: gid,
		ownerUID: os.Geteuid(), ownerGID: os.Getegid(),
		resources: make(map[string]JailResource),
	}
	if err := j.ensureDir("/"); err != nil {
		return nil, err
	}
	if err := j.aliasSocket(hostSocket, j.APISocketPath()); err != nil {
		return nil, fmt.Errorf("driver: stage jailed API socket: %w", err)
	}
	ready = true
	return j, nil
}

// APISocketPath is the path Firecracker receives inside the chroot.
func (j *Jail) APISocketPath() string { return "/api.sock" }

// VsockPath is the short path Firecracker receives inside the chroot. Keeping
// the device path independent of the host bundle prevents the jail root and
// thread directory from being repeated in sockaddr_un paths.
func (j *Jail) VsockPath() string { return "/vsock.sock" }

func (j *Jail) hostPath(jailPath string) (string, error) {
	clean := filepath.Clean("/" + jailPath)
	if clean == "/" {
		return j.RootDir, nil
	}
	host := filepath.Join(j.RootDir, strings.TrimPrefix(clean, "/"))
	rel, err := filepath.Rel(j.RootDir, host)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("jail path escapes root: %q", jailPath)
	}
	return host, nil
}

func (j *Jail) ensureDir(jailPath string) error {
	host, err := j.hostPath(jailPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(host, 0o750); err != nil {
		return err
	}
	for current := host; ; current = filepath.Dir(current) {
		if err := chownJailFile(current, j.uid, j.gid); err != nil {
			return fmt.Errorf("chown jail directory %q: %w", current, err)
		}
		if current == j.RootDir {
			break
		}
	}
	return nil
}

func (j *Jail) aliasSocket(hostAlias, jailPath string) error {
	target, err := j.hostPath(jailPath)
	if err != nil {
		return err
	}
	if err := j.ensureDir(filepath.Dir(jailPath)); err != nil {
		return err
	}
	if err := os.Remove(hostAlias); err != nil && !os.IsNotExist(err) {
		return err
	}
	return os.Symlink(target, hostAlias)
}

// aliasSocketPort stages one guest-initiated vsock connect target. Firecracker
// connects to <uds_path>_<port> inside its chroot, so aliasing only the base UDS
// cannot make a host listener reachable from the guest.
func (j *Jail) aliasSocketPort(hostBase, jailBase string, port uint32) error {
	suffix := fmt.Sprintf("_%d", port)
	return j.aliasSocket(hostBase+suffix, jailBase+suffix)
}

func (j *Jail) stageResource(r JailResource) (string, error) {
	if r.HostPath == "" || r.JailPath == "" {
		return "", fmt.Errorf("empty jail resource path")
	}
	j.mu.Lock()
	if existing, ok := j.resources[r.Role]; ok && existing.HostPath == r.HostPath && existing.JailPath == r.JailPath && existing.Writable == r.Writable {
		j.mu.Unlock()
		return existing.JailPath, nil
	}
	for _, existing := range j.resources {
		if existing.HostPath == r.HostPath && existing.JailPath == r.JailPath && existing.Writable == r.Writable && existing.PrivateCopy == r.PrivateCopy {
			j.resources[r.Role] = r
			j.mu.Unlock()
			return r.JailPath, nil
		}
	}
	j.mu.Unlock()
	if err := j.stageFile(r.Role, r.HostPath, r.JailPath, false, r.Writable, r.PrivateCopy); err != nil {
		return "", err
	}
	j.mu.Lock()
	j.resources[r.Role] = r
	j.mu.Unlock()
	return r.JailPath, nil
}

func (j *Jail) stageInput(role, hostPath, jailPath string, writable bool) (string, error) {
	return j.stageResource(JailResource{Role: role, HostPath: hostPath, JailPath: jailPath, Writable: writable})
}

func (j *Jail) stageOutput(hostPath, jailPath string) (string, error) {
	if err := j.stageFile("output", hostPath, jailPath, true, true, false); err != nil {
		return "", err
	}
	return jailPath, nil
}

func (j *Jail) stageFile(role, hostPath, jailPath string, create, writable, privateCopy bool) error {
	if !filepath.IsAbs(hostPath) {
		return fmt.Errorf("host path must be absolute: %q", hostPath)
	}
	if create {
		f, err := os.OpenFile(hostPath, os.O_CREATE|os.O_RDWR|os.O_TRUNC, 0o600)
		if err != nil {
			return err
		}
		if err := f.Close(); err != nil {
			return err
		}
	}
	device := strings.HasPrefix(filepath.Clean(hostPath), "/dev/")
	if !writable && !privateCopy && !device {
		// Settle owner and mode before the file is visible in the jail, so a
		// refusal never leaves a half-staged input behind.
		if err := j.prepareReadOnlyInput(role, hostPath); err != nil {
			return err
		}
	}
	target, err := j.hostPath(jailPath)
	if err != nil {
		return err
	}
	if err := j.ensureDir(filepath.Dir(jailPath)); err != nil {
		return err
	}
	if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
		return err
	}
	if privateCopy {
		if err := copyJailFile(hostPath, target); err != nil {
			return fmt.Errorf("copy %q into jail: %w", hostPath, err)
		}
		if err := chownJailFile(target, j.uid, j.gid); err != nil {
			return fmt.Errorf("chown private jail resource %q: %w", target, err)
		}
		if err := os.Chmod(target, 0o600); err != nil {
			return fmt.Errorf("chmod private jail resource %q: %w", target, err)
		}
		return nil
	}
	if err := os.Link(hostPath, target); err != nil {
		if !errors.Is(err, fs.ErrInvalid) && !errors.Is(err, os.ErrPermission) && !isCrossDevice(err) {
			return fmt.Errorf("hard link %q into jail: %w", hostPath, err)
		}
		if err := bindMountFile(hostPath, target, !writable); err != nil {
			return fmt.Errorf("bind mount %q into jail after hard-link failure: %w", hostPath, err)
		}
		j.mu.Lock()
		j.mounts = append(j.mounts, target)
		j.mu.Unlock()
	}
	if writable && !device {
		if err := chownJailFile(hostPath, j.uid, j.gid); err != nil {
			return fmt.Errorf("chown writable jail resource %q: %w", hostPath, err)
		}
		if err := chmodJailFile(hostPath, 0o600); err != nil {
			return fmt.Errorf("chmod writable jail resource %q: %w", hostPath, err)
		}
	}
	return nil
}

// prepareReadOnlyInput makes a shared read-only input readable by the jail uid
// without giving that uid the file. A pool uid that owned a node-shared base
// memfile could rewrite it for every later restore on the node, so an input
// left owned by another uid (a bundle captured while the jailer was briefly
// armed) is handed back to the daemon first. The jailed VMM then has only DAC
// bits, so a+r is added when, and only when, the jail uid and gid could not
// already read the file. An already readable input, such as the 0644 kernel on
// the image overlay, is never chmodded: no overlay copy-up, and no failure on a
// read-only mount.
func (j *Jail) prepareReadOnlyInput(role, hostPath string) error {
	info, err := os.Stat(hostPath)
	if err != nil {
		return fmt.Errorf("stat jail resource %s %q: %w", role, hostPath, err)
	}
	fileUID, fileGID, err := fileOwner(info)
	if err != nil {
		return fmt.Errorf("jail resource %s %q: %w", role, hostPath, err)
	}
	if fileUID != j.ownerUID {
		slog.Warn("driver: re-own jail input left by another uid",
			"role", role, "path", hostPath, "uid", fileUID, "owner_uid", j.ownerUID)
		if err := chownJailFile(hostPath, j.ownerUID, j.ownerGID); err != nil {
			return fmt.Errorf("re-own jail resource %s %q from uid %d: %w", role, hostPath, fileUID, err)
		}
		fileUID, fileGID = j.ownerUID, j.ownerGID
	}
	mode := info.Mode().Perm()
	if jailCanRead(mode, fileUID, fileGID, j.uid, j.gid) {
		return nil
	}
	if err := chmodJailFile(hostPath, mode|0o444); err != nil {
		return &JailResourceUnreadableError{Role: role, Path: hostPath, Mode: mode, UID: j.uid, GID: j.gid, Err: err}
	}
	// Re-check what the kernel now reports: a filesystem can accept a chmod
	// without applying it.
	if info, err = os.Stat(hostPath); err != nil {
		return fmt.Errorf("stat jail resource %s %q: %w", role, hostPath, err)
	}
	if fileUID, fileGID, err = fileOwner(info); err != nil {
		return fmt.Errorf("jail resource %s %q: %w", role, hostPath, err)
	}
	if mode = info.Mode().Perm(); !jailCanRead(mode, fileUID, fileGID, j.uid, j.gid) {
		return &JailResourceUnreadableError{Role: role, Path: hostPath, Mode: mode, UID: j.uid, GID: j.gid}
	}
	return nil
}

// jailCanRead reports whether a process running as exactly uid and gid, with
// no supplementary groups and no capabilities (Firecracker after the jailer
// drops privileges), may open a file of this mode and owner for reading. It
// follows the kernel's DAC class selection: the owner class when the uid
// matches, else the group class when the gid matches, else other. The matching
// class is final, so mode 0604 is unreadable to a member of the file's group.
func jailCanRead(mode fs.FileMode, fileUID, fileGID, uid, gid int) bool {
	perm := mode.Perm()
	switch {
	case uid == fileUID:
		return perm&0o400 != 0
	case gid == fileGID:
		return perm&0o040 != 0
	default:
		return perm&0o004 != 0
	}
}

func fileOwner(info fs.FileInfo) (uid, gid int, err error) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, fmt.Errorf("no owner in %T", info.Sys())
	}
	return int(stat.Uid), int(stat.Gid), nil
}

// JailResourceUnreadableError reports a read-only input the jailed VMM could
// not open. Staging returns it before Firecracker starts, in place of a virtio
// "Permission denied (os error 13)" from inside a running VMM.
type JailResourceUnreadableError struct {
	Role string
	Path string
	Mode fs.FileMode
	UID  int
	GID  int
	// Err is the failed chmod, or nil when the chmod took no effect.
	Err error
}

func (e *JailResourceUnreadableError) Error() string {
	msg := fmt.Sprintf("jail resource %s %q not readable by jail uid %d gid %d (mode %04o)",
		e.Role, e.Path, e.UID, e.GID, uint32(e.Mode.Perm()))
	if e.Err != nil {
		msg += ": " + e.Err.Error()
	}
	return msg
}

func (e *JailResourceUnreadableError) Unwrap() error { return e.Err }

// releaseOutput hands a snapshot output back to the daemon and removes its jail
// alias. stageOutput chowned the host file to the jail uid so Firecracker could
// create it; leaving it there would let any later VMM that reuses the uid
// (every brick and every restart counts from 20000) rewrite a node-shared base.
// The alias would also keep a memMib-sized inode alive after an aborted
// checkpoint removes the host name. A descriptor that a timed-out call still
// holds keeps working, because permission is checked only at open.
func (j *Jail) releaseOutput(hostPath, jailPath string) error {
	var releaseErr error
	if target, err := j.hostPath(jailPath); err != nil {
		releaseErr = err
	} else {
		if j.forgetMount(target) {
			if err := unmountFile(target); err != nil {
				releaseErr = errors.Join(releaseErr, fmt.Errorf("unmount snapshot output alias %q: %w", target, err))
			}
		}
		if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
			releaseErr = errors.Join(releaseErr, fmt.Errorf("remove snapshot output alias %q: %w", target, err))
		}
	}
	if err := chownJailFile(hostPath, j.ownerUID, j.ownerGID); err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			releaseErr = errors.Join(releaseErr, fmt.Errorf("re-own snapshot output %q: %w", hostPath, err))
		}
	} else if err := chmodJailFile(hostPath, 0o600); err != nil && !errors.Is(err, fs.ErrNotExist) {
		releaseErr = errors.Join(releaseErr, fmt.Errorf("chmod snapshot output %q: %w", hostPath, err))
	}
	return releaseErr
}

// forgetMount drops target from the bind-mount fallbacks and reports whether
// it was one, so the caller unmounts it before removing the alias.
func (j *Jail) forgetMount(target string) bool {
	j.mu.Lock()
	defer j.mu.Unlock()
	for i, mount := range j.mounts {
		if mount == target {
			j.mounts = append(j.mounts[:i], j.mounts[i+1:]...)
			return true
		}
	}
	return false
}

func copyJailFile(source, target string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return err
	}
	return out.Close()
}

func isCrossDevice(err error) bool {
	return errors.Is(err, syscall.EXDEV)
}

// jailResourceGID preserves device-node ownership. Device permissions are
// managed by devmapper and udev, not per VM, so a jailed process joins that
// existing group instead of chowning the shared node to its transient uid.
func jailResourceGID(resources []JailResource, fallback int) int {
	for _, resource := range resources {
		if !strings.HasPrefix(filepath.Clean(resource.HostPath), "/dev/") {
			continue
		}
		info, err := os.Stat(resource.HostPath)
		if err != nil {
			continue
		}
		if stat, ok := info.Sys().(*syscall.Stat_t); ok {
			return int(stat.Gid)
		}
	}
	return fallback
}

func (j *Jail) Resources() []JailResource {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]JailResource, 0, len(j.resources))
	for _, r := range j.resources {
		out = append(out, r)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Role < out[k].Role })
	return out
}

func (j *Jail) CleanupMounts() error {
	j.mu.Lock()
	mounts := append([]string(nil), j.mounts...)
	j.mounts = nil
	j.mu.Unlock()
	var cleanupErr error
	for i := len(mounts) - 1; i >= 0; i-- {
		if err := unmountFile(mounts[i]); err != nil {
			cleanupErr = errors.Join(cleanupErr, fmt.Errorf("unmount %q: %w", mounts[i], err))
		}
	}
	return cleanupErr
}

// Cleanup unmounts bind-mounted fallbacks before removing the complete per-VM
// jail tree. The ordering prevents RemoveAll from failing with EBUSY.
func (j *Jail) Cleanup() error {
	unmountErr := j.CleanupMounts()
	removeErr := os.RemoveAll(j.Dir)
	return errors.Join(unmountErr, removeErr)
}

func writeJailResources(dir string, resources []JailResource) error {
	if len(resources) == 0 {
		return nil
	}
	b, err := json.Marshal(resources)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, jailResourcesName), b, 0o600)
}

func readJailResources(dir string) ([]JailResource, bool, error) {
	b, err := os.ReadFile(filepath.Join(dir, jailResourcesName))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, true, err
	}
	var resources []JailResource
	if err := json.Unmarshal(b, &resources); err != nil {
		return nil, true, fmt.Errorf("decode %s: %w", jailResourcesName, err)
	}
	if len(resources) == 0 {
		return nil, true, fmt.Errorf("%s contains no resources", jailResourcesName)
	}
	return resources, true, nil
}

func snapshotJailPath(hostPath, kind string) string {
	sum := sha256.Sum256([]byte(hostPath))
	return filepath.Join("/snapshots", hex.EncodeToString(sum[:8])+"-"+kind)
}

type jailedClient struct {
	fcAPI
	jail *Jail
}

func (c *jailedClient) PutBootSource(ctx context.Context, b fcclient.BootSource) error {
	path, err := c.jail.stageInput("kernel", b.KernelImagePath, "/kernel", false)
	if err != nil {
		return err
	}
	b.KernelImagePath = path
	if b.InitrdPath != "" {
		path, err = c.jail.stageInput("initrd", b.InitrdPath, "/initrd", false)
		if err != nil {
			return err
		}
		b.InitrdPath = path
	}
	return c.fcAPI.PutBootSource(ctx, b)
}

func (c *jailedClient) PutDrive(ctx context.Context, d fcclient.Drive) error {
	// Keep the absolute spelling embedded in snapshots, but stage that name below
	// RootDir. Firecracker resolves it inside the chroot, while a snapshot loaded
	// through the direct-exec escape hatch can still resolve the same host path.
	path, err := c.jail.stageInput(d.DriveID, d.PathOnHost, d.PathOnHost, !d.IsReadOnly)
	if err != nil {
		return err
	}
	d.PathOnHost = path
	return c.fcAPI.PutDrive(ctx, d)
}

func (c *jailedClient) PatchDrive(ctx context.Context, driveID string, d fcclient.PatchedDrive) error {
	path, err := c.jail.stageInput(driveID, d.PathOnHost, d.PathOnHost, true)
	if err != nil {
		return err
	}
	d.PathOnHost = path
	return c.fcAPI.PatchDrive(ctx, driveID, d)
}

func (c *jailedClient) PutVsock(ctx context.Context, v fcclient.Vsock) error {
	return c.fcAPI.PutVsock(ctx, v)
}

func (c *jailedClient) PutSerial(ctx context.Context, s fcclient.Serial) error {
	path, err := c.jail.stageInput("serial", s.SerialOutPath, "/serial.log", true)
	if err != nil {
		return err
	}
	s.SerialOutPath = path
	return c.fcAPI.PutSerial(ctx, s)
}

// CreateSnapshot stages both outputs writable for the jail uid, then releases
// them on every outcome: success, a stage failure, an API error or a timeout.
// A release failure fails the snapshot, so no caller publishes an output that a
// pool uid still owns.
func (c *jailedClient) CreateSnapshot(ctx context.Context, s fcclient.SnapshotCreate) (err error) {
	snapHost, memHost := s.SnapshotPath, s.MemFilePath
	snapJail := snapshotJailPath(snapHost, "state")
	memJail := snapshotJailPath(memHost, "memory")
	defer func() {
		if releaseErr := errors.Join(c.jail.releaseOutput(snapHost, snapJail), c.jail.releaseOutput(memHost, memJail)); releaseErr != nil {
			err = errors.Join(err, fmt.Errorf("driver: release jailed snapshot outputs: %w", releaseErr))
		}
	}()
	snap, err := c.jail.stageOutput(snapHost, snapJail)
	if err != nil {
		return err
	}
	mem, err := c.jail.stageOutput(memHost, memJail)
	if err != nil {
		return err
	}
	s.SnapshotPath = snap
	s.MemFilePath = mem
	return c.fcAPI.CreateSnapshot(ctx, s)
}

func (c *jailedClient) LoadSnapshot(ctx context.Context, s fcclient.SnapshotLoad) error {
	snap, err := c.jail.stageInput("snapshot-state", s.SnapshotPath, "/snapshot/snapfile", false)
	if err != nil {
		return err
	}
	s.SnapshotPath = snap
	if s.MemBackend != nil {
		mem, err := c.jail.stageInput("snapshot-memory", s.MemBackend.BackendPath, "/snapshot/memfile", false)
		if err != nil {
			return err
		}
		backend := *s.MemBackend
		backend.BackendPath = mem
		s.MemBackend = &backend
	}
	return c.fcAPI.LoadSnapshot(ctx, s)
}
