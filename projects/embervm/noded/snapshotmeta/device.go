// Package snapshotmeta reads structural metadata persisted beside Firecracker
// snapshots. It deliberately contains no restore policy: callers decide how an
// unknown legacy shape may be used.
package snapshotmeta

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
)

const JailResourcesFile = "jail-resources.json"

// DeviceSet is the captured non-root drive set. Known is false only when a
// legacy bundle has no jail resource metadata. IDs is sorted and deduplicated.
type DeviceSet struct {
	Known bool
	IDs   []string
}

type jailResource struct {
	Role string `json:"role"`
}

// DeviceSetFromJailResources derives Firecracker drive IDs from the metadata
// written by the snapshot producer. Resource roles intentionally match drive
// IDs for persisted drives; rootfs is excluded because every base has it.
func DeviceSetFromJailResources(data []byte) (DeviceSet, error) {
	var resources []jailResource
	if err := json.Unmarshal(data, &resources); err != nil {
		return DeviceSet{Known: true}, fmt.Errorf("decode %s: %w", JailResourcesFile, err)
	}
	if len(resources) == 0 {
		return DeviceSet{Known: true}, fmt.Errorf("%s contains no resources", JailResourcesFile)
	}
	seen := make(map[string]struct{}, len(resources))
	hasRootfs := false
	for _, resource := range resources {
		if resource.Role == "" {
			return DeviceSet{Known: true}, fmt.Errorf("%s contains a resource without a role", JailResourcesFile)
		}
		if resource.Role == "rootfs" {
			hasRootfs = true
			continue
		}
		seen[resource.Role] = struct{}{}
	}
	if !hasRootfs {
		return DeviceSet{Known: true}, fmt.Errorf("%s contains no rootfs drive", JailResourcesFile)
	}
	ids := make([]string, 0, len(seen))
	for id := range seen {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return DeviceSet{Known: true, IDs: ids}, nil
}

// ReadDeviceSet reads the producer-owned metadata in dir. A missing file is a
// valid legacy state and returns Known=false; malformed metadata is an error.
func ReadDeviceSet(dir string) (DeviceSet, error) {
	data, err := os.ReadFile(filepath.Join(dir, JailResourcesFile))
	if errors.Is(err, os.ErrNotExist) {
		return DeviceSet{}, nil
	}
	if err != nil {
		return DeviceSet{Known: true}, err
	}
	return DeviceSetFromJailResources(data)
}

func (d DeviceSet) Has(id string) bool {
	return d.Known && slices.Contains(d.IDs, id)
}

// Equal compares both certainty and shape. Two unknown legacy sets are equal,
// but neither is proof that a replacement has the same device table.
func (d DeviceSet) Equal(other DeviceSet) bool {
	return d.Known == other.Known && slices.Equal(d.IDs, other.IDs)
}

func (d DeviceSet) String() string {
	if !d.Known {
		return "unknown (legacy metadata absent)"
	}
	return fmt.Sprintf("known %v", d.IDs)
}
