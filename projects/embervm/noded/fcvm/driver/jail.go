package driver

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
)

const jailResourcesName = "jail-resources.json"

// JailResource records one host backing file and the path embedded in the
// Firecracker device model. Persisting this beside a snapshot lets a later jail
// stage exactly the files that snapshot will reopen.
type JailResource struct {
	Role     string `json:"role"`
	HostPath string `json:"host_path"`
	JailPath string `json:"jail_path"`
	Writable bool   `json:"writable,omitempty"`
}

// Jail maps noded-visible paths to the private filesystem seen by Firecracker.
type Jail struct {
	BaseDir string
	RootDir string
	uid     int
	gid     int

	mu        sync.Mutex
	resources map[string]JailResource
	mounts    []string
}

func prepareJail(bundleDir, execFile, vmID string, uid, gid int, hostSocket string) (*Jail, error) {
	base := filepath.Join(bundleDir, "jailer")
	root := filepath.Join(base, filepath.Base(execFile), vmID, "root")
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, fmt.Errorf("driver: create jail root: %w", err)
	}
	j := &Jail{BaseDir: base, RootDir: root, uid: uid, gid: gid, resources: make(map[string]JailResource)}
	if err := j.ensureDir("/"); err != nil {
		return nil, err
	}
	if err := j.aliasSocket(hostSocket, j.APISocketPath()); err != nil {
		return nil, fmt.Errorf("driver: stage jailed API socket: %w", err)
	}
	return j, nil
}

// APISocketPath is the path Firecracker receives inside the chroot.
func (j *Jail) APISocketPath() string { return "/api.sock" }

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
		if err := os.Chown(current, j.uid, j.gid); err != nil {
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

func (j *Jail) stageResource(r JailResource) (string, error) {
	if r.HostPath == "" || r.JailPath == "" {
		return "", fmt.Errorf("empty jail resource path")
	}
	j.mu.Lock()
	for _, existing := range j.resources {
		if existing.HostPath == r.HostPath && existing.JailPath == r.JailPath && existing.Writable == r.Writable {
			j.resources[r.Role] = r
			j.mu.Unlock()
			return r.JailPath, nil
		}
	}
	j.mu.Unlock()
	if err := j.stageFile(r.HostPath, r.JailPath, false, r.Writable); err != nil {
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
	if err := j.stageFile(hostPath, jailPath, true, true); err != nil {
		return "", err
	}
	return jailPath, nil
}

func (j *Jail) stageFile(hostPath, jailPath string, create, writable bool) error {
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
	if writable {
		if err := os.Chown(hostPath, j.uid, j.gid); err != nil {
			return fmt.Errorf("chown writable jail resource %q: %w", hostPath, err)
		}
		if err := os.Chmod(hostPath, 0o600); err != nil {
			return fmt.Errorf("chmod writable jail resource %q: %w", hostPath, err)
		}
	} else {
		info, err := os.Stat(hostPath)
		if err != nil {
			return err
		}
		if err := os.Chmod(hostPath, info.Mode().Perm()|0o444); err != nil {
			return fmt.Errorf("make jail resource readable %q: %w", hostPath, err)
		}
	}
	return nil
}

func isCrossDevice(err error) bool {
	return errors.Is(err, syscall.EXDEV)
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

func (j *Jail) CleanupMounts() {
	j.mu.Lock()
	mounts := append([]string(nil), j.mounts...)
	j.mounts = nil
	j.mu.Unlock()
	for i := len(mounts) - 1; i >= 0; i-- {
		_ = unmountFile(mounts[i])
	}
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

func readJailResources(dir string) []JailResource {
	b, err := os.ReadFile(filepath.Join(dir, jailResourcesName))
	if err != nil {
		return nil
	}
	var resources []JailResource
	if json.Unmarshal(b, &resources) != nil {
		return nil
	}
	return resources
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

func (c *jailedClient) CreateSnapshot(ctx context.Context, s fcclient.SnapshotCreate) error {
	snap, err := c.jail.stageOutput(s.SnapshotPath, snapshotJailPath(s.SnapshotPath, "state"))
	if err != nil {
		return err
	}
	mem, err := c.jail.stageOutput(s.MemFilePath, snapshotJailPath(s.MemFilePath, "memory"))
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
