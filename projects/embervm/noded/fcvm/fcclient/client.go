// Package fcclient is a minimal Firecracker REST API client over the microVM's
// unix domain API socket. It implements exactly the calls the snapshot/restore
// controller needs (machine config, boot source, drives, vsock, start, pause,
// resume, snapshot create/load), following the FC-direct approach ported from
// e2b-dev/infra rather than pulling in the full firecracker-go-sdk.
//
// The Firecracker API speaks HTTP/1.1 over a unix socket and returns 204 No
// Content on success, or a JSON {"fault_message": "..."} body on error.
package fcclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// Client talks to a single Firecracker process over its API socket.
type Client struct {
	http       *http.Client
	socketPath string
}

// New returns a client bound to the Firecracker API socket at socketPath.
func New(socketPath string) *Client {
	return &Client{
		socketPath: socketPath,
		http: &http.Client{
			Timeout: 30 * time.Second,
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					var d net.Dialer
					return d.DialContext(ctx, "unix", socketPath)
				},
			},
		},
	}
}

// MachineConfig is the body of PUT /machine-config.
type MachineConfig struct {
	VCPUCount  int  `json:"vcpu_count"`
	MemSizeMib int  `json:"mem_size_mib"`
	SMT        bool `json:"smt"`
}

// BootSource is the body of PUT /boot-source.
type BootSource struct {
	KernelImagePath string `json:"kernel_image_path"`
	BootArgs        string `json:"boot_args,omitempty"`
	InitrdPath      string `json:"initrd_path,omitempty"`
}

// Drive is the body of PUT /drives/{drive_id}.
type Drive struct {
	DriveID      string `json:"drive_id"`
	PathOnHost   string `json:"path_on_host"`
	IsRootDevice bool   `json:"is_root_device"`
	IsReadOnly   bool   `json:"is_read_only"`
}

// PatchedDrive is the body of PATCH /drives/{drive_id}. Firecracker's PATCH
// schema is a partial update accepting ONLY drive_id, path_on_host, and
// rate_limiter, and it rejects unknown fields at JSON parse, so this cannot
// reuse Drive: is_root_device fails the whole request with a SerdeJson error
// before the drive is even looked up.
type PatchedDrive struct {
	DriveID    string `json:"drive_id"`
	PathOnHost string `json:"path_on_host"`
}

// Vsock is the body of PUT /vsock. The controller uses a vsock device for the
// in-VM wrapper's idle-signal channel (Phase 2).
type Vsock struct {
	GuestCID int    `json:"guest_cid"`
	UDSPath  string `json:"uds_path"`
}

// NetworkInterface is the body of PUT /network-interfaces/{iface_id}. It is used
// ONLY by serving-class VMs (R3): the daemon creates a host tap device and attaches
// it to the guest as eth0 so the guest answers HTTP directly over an L3 NIC. Task and
// session VMs are vsock-only and NEVER configure a network interface, so this call is
// absent from their boot path. It is a PRE-BOOT config call (like drives and vsock):
// Firecracker cannot hot-attach a NIC to a snapshot-restored/resumed VM, so a serving
// VM gets its NIC at cold boot and a serving snapshot resume keeps the NIC that was
// captured in the snapshot.
type NetworkInterface struct {
	IfaceID     string `json:"iface_id"`
	HostDevName string `json:"host_dev_name"`
	GuestMAC    string `json:"guest_mac,omitempty"`
}

// MemBackend selects how a memory snapshot is restored. Backend "File" mmaps the
// image and faults pages lazily (sub-second restore without UFFD); "Uffd" uses a
// userfaultfd handler (Phase 6).
type MemBackend struct {
	BackendType string `json:"backend_type"`
	BackendPath string `json:"backend_path"`
}

// SnapshotCreate is the body of PUT /snapshot/create.
type SnapshotCreate struct {
	SnapshotType string `json:"snapshot_type,omitempty"` // "Full" (default) or "Diff"
	SnapshotPath string `json:"snapshot_path"`
	MemFilePath  string `json:"mem_file_path"`
}

// SnapshotLoad is the body of PUT /snapshot/load.
type SnapshotLoad struct {
	SnapshotPath        string      `json:"snapshot_path"`
	MemBackend          *MemBackend `json:"mem_backend,omitempty"`
	EnableDiffSnapshots bool        `json:"enable_diff_snapshots,omitempty"`
	ResumeVM            bool        `json:"resume_vm"`
}

// PutMachineConfig configures vCPUs and memory.
func (c *Client) PutMachineConfig(ctx context.Context, m MachineConfig) error {
	return c.do(ctx, http.MethodPut, "/machine-config", m)
}

// PutBootSource sets the guest kernel and boot args.
func (c *Client) PutBootSource(ctx context.Context, b BootSource) error {
	return c.do(ctx, http.MethodPut, "/boot-source", b)
}

// PutDrive attaches a block device (the devmapper rootfs).
func (c *Client) PutDrive(ctx context.Context, d Drive) error {
	return c.do(ctx, http.MethodPut, "/drives/"+d.DriveID, d)
}

// PatchDrive updates a drive's path_on_host on a loaded (not-yet-resumed)
// microVM. Used to repoint the volume drive to a session-specific backing file
// after LoadSnapshot on the warm-restore path. The drive must already exist in
// the snapshot's device set.
func (c *Client) PatchDrive(ctx context.Context, driveID string, d PatchedDrive) error {
	return c.do(ctx, http.MethodPatch, "/drives/"+driveID, d)
}

// PutVsock attaches a vsock device for the wrapper channel.
func (c *Client) PutVsock(ctx context.Context, v Vsock) error {
	return c.do(ctx, http.MethodPut, "/vsock", v)
}

// PutNetworkInterface attaches a host tap device to the guest as a network
// interface. Serving-class VMs only; a pre-boot call (see NetworkInterface).
func (c *Client) PutNetworkInterface(ctx context.Context, n NetworkInterface) error {
	return c.do(ctx, http.MethodPut, "/network-interfaces/"+n.IfaceID, n)
}

// Start boots the configured microVM (action InstanceStart).
func (c *Client) Start(ctx context.Context) error {
	return c.do(ctx, http.MethodPut, "/actions", map[string]string{"action_type": "InstanceStart"})
}

// Pause pauses the running microVM so a consistent snapshot can be taken.
func (c *Client) Pause(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", map[string]string{"state": "Paused"})
}

// Resume resumes a paused microVM.
func (c *Client) Resume(ctx context.Context) error {
	return c.do(ctx, http.MethodPatch, "/vm", map[string]string{"state": "Resumed"})
}

// CreateSnapshot writes the snapfile + memfile for the (paused) microVM.
func (c *Client) CreateSnapshot(ctx context.Context, s SnapshotCreate) error {
	return c.do(ctx, http.MethodPut, "/snapshot/create", s)
}

// LoadSnapshot restores a microVM from a snapshot bundle into this (fresh,
// not-yet-started) Firecracker process, optionally resuming it.
func (c *Client) LoadSnapshot(ctx context.Context, s SnapshotLoad) error {
	return c.do(ctx, http.MethodPut, "/snapshot/load", s)
}

func (c *Client) do(ctx context.Context, method, path string, body any) error {
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			return fmt.Errorf("fcclient: encode %s %s: %w", method, path, err)
		}
	}
	// The host is ignored (unix socket) but must be a valid URL.
	req, err := http.NewRequestWithContext(ctx, method, "http://localhost"+path, &buf)
	if err != nil {
		return fmt.Errorf("fcclient: new request %s %s: %w", method, path, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("fcclient: %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	return fmt.Errorf("fcclient: %s %s: status %d: %s", method, path, resp.StatusCode, bytes.TrimSpace(msg))
}
