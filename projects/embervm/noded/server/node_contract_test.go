package server

import (
	"bytes"
	"testing"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// The control plane and daemon upgrade separately. Retired names AND tags stay
// reserved, and every surviving tag in a touched message retains its identity.
func TestNodeContractRetiredFieldsAndWireTags(t *testing.T) {
	cases := []struct {
		message proto.Message
		retired map[protoreflect.Name]protoreflect.FieldNumber
		live    map[protoreflect.Name]protoreflect.FieldNumber
	}{
		{
			&nodev1.Trace{Workload: "w", TaskId: "t"},
			map[protoreflect.Name]protoreflect.FieldNumber{"correlation_id": 3},
			map[protoreflect.Name]protoreflect.FieldNumber{"workload": 1, "task_id": 2},
		},
		{
			&nodev1.BuildBaseRequest{ImageRef: "image", ReadyPath: "/ready"},
			map[protoreflect.Name]protoreflect.FieldNumber{"guest_port": 4},
			map[protoreflect.Name]protoreflect.FieldNumber{"trace": 1, "image_ref": 2, "workload_revision": 3, "ready_path": 5, "resources": 6, "init_env": 7, "zip": 8, "serving": 9},
		},
		{
			&nodev1.PrimeRequest{SnapshotRef: "base", LineageId: "lineage", VolumeMount: "/workspace", VolumeSizeBytes: 4096},
			map[protoreflect.Name]protoreflect.FieldNumber{"volume_disk_path": 3},
			map[protoreflect.Name]protoreflect.FieldNumber{"trace": 1, "snapshot_ref": 2, "volume_mount": 4, "volume_size_bytes": 5, "lineage_id": 6},
		},
		{
			&nodev1.SessionAssignRequest{VmId: "vm", SessionId: "session", LineageId: "lineage", DispatchId: "dispatch"},
			map[protoreflect.Name]protoreflect.FieldNumber{"volume_disk_path": 6},
			map[protoreflect.Name]protoreflect.FieldNumber{"trace": 1, "vm_id": 2, "request": 3, "timeout_ms": 4, "session_id": 5, "volume_mount": 7, "volume_size_bytes": 8, "lineage_id": 9, "dispatch_id": 10},
		},
		{
			&nodev1.RegistryEntry{Workload: "w", RootfsRef: "/rootfs", StatefulVolumeMount: "/data"},
			map[protoreflect.Name]protoreflect.FieldNumber{"stateful_boot_image_ref": 13, "stateful_volume_device": 14},
			map[protoreflect.Name]protoreflect.FieldNumber{"workload": 1, "image_digest": 2, "rootfs_ref": 3, "harness_init": 4, "sizing": 5, "image_ref": 6, "node_local_wake": 7, "serving_port": 8, "serving_health_path": 9, "stateful_listen_port": 10, "stateful_port": 11, "stateful_volume_mount": 12, "group_listen_port": 15, "group_member_plan": 16, "blessing_leases": 17},
		},
		{
			&nodev1.StartStatefulRequest{BootImageRef: "base", BlessedGeneration: 42},
			map[protoreflect.Name]protoreflect.FieldNumber{"volume_device": 12},
			map[protoreflect.Name]protoreflect.FieldNumber{"trace": 1, "mode": 2, "boot_image_ref": 3, "relight_snapshot_ref": 4, "port": 5, "volume_size_bytes": 6, "volume_mount": 7, "create_if_missing": 8, "resources": 9, "mmds_env": 10, "blessed_generation": 11},
		},
		{
			&nodev1.NodeStatus{NodeId: "node", CpuVendor: "amd", BuildError: "diagnostic"},
			map[protoreflect.Name]protoreflect.FieldNumber{"node_template_hash": 25},
			map[protoreflect.Name]protoreflect.FieldNumber{
				"node_id": 1, "workloads": 2, "mem_headroom_mib": 3, "cpu_headroom_millicores": 4,
				"live_vms": 5, "max_live_vms": 6, "draining": 7, "build_error": 8,
				"session_vms": 9, "session_snapshots": 10, "snapshot_disk_free_bytes": 11, "snapshot_disk_used_bytes": 12,
				"serving_vms": 13, "serving_snapshots": 14, "serving_subnet_cidr": 15, "stateful_vms": 16,
				"stateful_bundles": 17, "volumes": 18, "group_networks": 19, "group_member_vms": 20,
				"group_bundle_sets": 21, "drain_deadline_unix_ms": 22, "store_reachable": 23, "cpu_vendor": 24,
				"mem_budget_mib": 26, "cpu_budget_millicores": 27, "cpu_sku": 28, "pod_uid": 29, "size_class": 30,
				"local_bases": 31, "activator_endpoint": 32, "activator_ip": 33, "mem_reject_floor_mib": 34,
				"mem_reserved_mib": 35, "admits_on_reservation": 36, "vm_overhead_mib": 37, "session_volumes": 38,
				"scratch_generation": 39, "vmm_exit_counts": 40, "guest_memory_state_counts": 41, "guest_oom_count": 42,
				"session_volumes_complete": 43, "admission_reported": 44, "mem_admittable_mib": 45,
				"mem_inflight_mib": 46, "inflight_vms": 47, "admission_refusals": 48, "jailer_armed": 49,
				"launch_modes": 50, "mem_inflight_placed_mib": 51, "inflight_placed_vms": 52,
			},
		},
	}
	for _, tc := range cases {
		d := tc.message.ProtoReflect().Descriptor()
		t.Run(string(d.Name()), func(t *testing.T) {
			for name, tag := range tc.live {
				field := d.Fields().ByName(name)
				if field == nil || field.Number() != tag {
					t.Errorf("surviving %s must keep tag %d; got %v", name, tag, field)
				}
			}
			for name, tag := range tc.retired {
				if !d.ReservedNames().Has(name) || !d.ReservedRanges().Has(tag) || d.Fields().ByName(name) != nil || d.Fields().ByNumber(tag) != nil {
					t.Fatalf("retired %s/%d must reserve both name and tag", name, tag)
				}
				// Simulate an older sender appending its formerly known field. The
				// new reader preserves it as unknown without altering live fields.
				var unknown []byte
				if name == "guest_port" {
					unknown = protowire.AppendVarint(protowire.AppendTag(nil, tag, protowire.VarintType), 9090)
				} else {
					unknown = protowire.AppendString(protowire.AppendTag(nil, tag, protowire.BytesType), "retired-value")
				}
				wire, err := proto.Marshal(tc.message)
				if err != nil {
					t.Fatal(err)
				}
				decoded := tc.message.ProtoReflect().New().Interface()
				if err := proto.Unmarshal(append(wire, unknown...), decoded); err != nil {
					t.Fatal(err)
				}
				if !bytes.Equal(decoded.ProtoReflect().GetUnknown(), unknown) {
					t.Fatal("legacy field was not retained as unknown wire data")
				}
				reencoded, err := proto.Marshal(decoded)
				if err != nil {
					t.Fatal(err)
				}
				if err := proto.Unmarshal(reencoded, decoded); err != nil {
					t.Fatal(err)
				}
				if !bytes.Equal(decoded.ProtoReflect().GetUnknown(), unknown) {
					t.Fatal("unknown wire data lost on re-encode")
				}
				decoded.ProtoReflect().SetUnknown(nil)
				if !proto.Equal(decoded, tc.message) {
					t.Fatalf("legacy field changed current fields: got %v, want %v", decoded, tc.message)
				}
			}
		})
	}
}

func TestNodeContractRetiredRPCsAndRetainedConvenience(t *testing.T) {
	file := (&nodev1.Trace{}).ProtoReflect().Descriptor().ParentFile()
	service := file.Services().ByName("NodeService")
	for _, name := range []protoreflect.Name{"RegisterWorkload", "DeregisterWorkload"} {
		if service.Methods().ByName(name) != nil || file.Messages().ByName(name+"Request") != nil || file.Messages().ByName(name+"Response") != nil {
			t.Errorf("retired RPC or message %s remains", name)
		}
	}
	for _, name := range []protoreflect.Name{"SyncRegistry", "GetNodeStatus", "WatchNode"} {
		if service.Methods().ByName(name) == nil {
			t.Errorf("retained RPC %s missing", name)
		}
	}
	if file.Enums().ByName("GroupGuestAgent") != nil {
		t.Error("unused GroupGuestAgent enum remains")
	}
	for message, tag := range map[protoreflect.Name]protoreflect.FieldNumber{"ServingVm": 8, "StatefulVm": 11} {
		field := file.Messages().ByName(message).Fields().ByName("grant_id")
		if field == nil || field.Number() != tag {
			t.Errorf("%s.grant_id must retain tag %d", message, tag)
		}
	}
	if nodev1.BaseBuildState_BASE_BUILD_STATE_FAILED != 4 {
		t.Fatal("FAILED state changed its wire value")
	}
}
