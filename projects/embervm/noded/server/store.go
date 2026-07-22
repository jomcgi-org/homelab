package server

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// artifactStore is the seam the R6 continuity verbs and the async export queue
// depend on: the off-node object-store operations over a Fork-3 key prefix. The
// real *store.Store satisfies it; tests inject an in-memory fake so no test
// touches the network. It is a SEPARATE seam (like sessionDriver/servingDriver)
// so a Server built without a store still compiles: a nil artifactStore leaves
// ExportArtifact/RestoreArtifact refusing FAILED_PRECONDITION and every export a
// no-op, and NodeStatus.store_reachable false.
//
// Method shapes mirror *store.Store exactly. errNotPresent is the sentinel a
// Restore/Present returns for an absent store copy; the handlers map it to
// FAILED_PRECONDITION. A store package cannot be imported for its sentinel here
// (the fake would then need it too), so the seam declares the sentinel it cares
// about via a small predicate the store satisfies.
type artifactStore interface {
	Export(ctx context.Context, prefix, localDir string, files []string, generation uint64, nowMs int64, cpuVendor, cpuTemplate string) (bytesMoved int64, skipped bool, err error)
	Restore(ctx context.Context, prefix, localDir string) (bytesMoved int64, generation uint64, err error)
	DeleteArtifact(ctx context.Context, prefix string) error
	Present(ctx context.Context, prefix string) (present bool, generation uint64, cpuVendor, cpuTemplate string, err error)
	Reachable(ctx context.Context) bool
}

// artifactKindStr maps an ArtifactKind to its lowercase store-key segment (Fork
// 3, extended by R7 standing decision 11: <kindStr>/<workload>/<ref> for VOLUME,
// <kindStr>/<vendor>/<workload>/<ref> for every other kind). Returns "" for the
// unspecified kind so a caller refuses an unknown ref rather than composing a
// bogus prefix.
func artifactKindStr(kind nodev1.ArtifactKind) string {
	switch kind {
	case nodev1.ArtifactKind_ARTIFACT_KIND_BASE:
		return "base"
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION:
		return "session"
	case nodev1.ArtifactKind_ARTIFACT_KIND_SERVING:
		return "serving"
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		return "stateful"
	case nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET:
		return "group_set"
	case nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME:
		return "volume"
	default:
		return ""
	}
}

// legacyVendorAlias is the vendor a pre-R7 (un-vendored) store artifact is
// treated as (the node-4 alias, standing decision 11): every artifact exported
// before vendor keying shipped was exported from node-4 (AMD), the only node in
// the fleet at the time. It mirrors the driver package's constant of the same
// name and value; the two live in separate layers (store-key resolution here,
// snapshot-restore validation there) so neither imports the other for one
// string constant.
const legacyVendorAlias = "amd"

// cpuSkuMismatch is the PR-E fail-closed gate: it reports whether a stamped
// artifact's cpu_sku conflicts with this node's own, plus the two strings a
// caller's error message reports (got: what the artifact carried, want: what
// this node is).
//
// Grandfather rule (ADR embervm/012; a missing stamp must NEVER be refused,
// refusing a grandfathered artifact is data loss):
//   - stampedVendor == "" AND stampedTemplate == "" (no stamp at all: the
//     artifact was exported before PR-E) -> UNSTAMPED, always compatible,
//     never a mismatch, regardless of this node's own sku.
//   - a stamp is present (either field non-empty) and differs from this
//     node's own vendor or template -> MISMATCH, refused loudly.
//   - a stamp is present and matches this node's own vendor AND template ->
//     compatible.
//
// This node's own sku being unresolved (nodeVendor == "", an undetected
// vendor) skips the check entirely regardless of the artifact's stamp,
// mirroring how an empty node vendor already skips the vendor-only check: a
// node that cannot state its own identity cannot judge a mismatch, so it
// never refuses on this basis (the existing vendor-mismatch gate in
// resolveRestorePrefix, unchanged, is the layer that already requires a vendor
// to reach this far in the request-vendor case; this is the artifact-stamp
// layer, checked independently against the LOCAL node config).
func cpuSkuMismatch(stampedVendor, stampedTemplate, nodeVendor, nodeTemplate string) (mismatch bool, got, want string) {
	want = nodeVendor + "/" + nodeTemplate
	if nodeVendor == "" {
		return false, stampedVendor + "/" + stampedTemplate, want
	}
	if stampedVendor == "" && stampedTemplate == "" {
		// UNSTAMPED: grandfathered legacy artifact, always compatible.
		return false, "", want
	}
	got = stampedVendor + "/" + stampedTemplate
	if stampedVendor != nodeVendor || stampedTemplate != nodeTemplate {
		return true, got, want
	}
	return false, got, want
}

// artifactVendorSegment reports whether kind is one of the vendor-bound kinds
// (BASE, SESSION, SERVING, STATEFUL, GROUP_SET) that carries a vendor segment in
// its store key (R7 standing decision 11). VOLUME data is fully portable across
// vendors (standing decision 1) and is deliberately excluded: its key never
// gains a vendor segment, so a volume exported from one node restores cleanly
// onto any other regardless of CPU vendor.
func artifactVendorSegment(kind nodev1.ArtifactKind) bool {
	return kind != nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME
}

// artifactPrefix composes the store key prefix for a ref. VOLUME collapses to
// volume/<workload> (the ref MAY be empty: the volume is a singleton per
// workload) with no vendor segment, since volume data is vendor-portable
// (standing decision 1). Every other kind is vendor-bound (standing decision
// 11) and keys as <kindStr>/<vendor>/<workload>/<ref>; an empty vendor for a
// vendor-bound kind is refused ("") rather than silently omitting the segment,
// so a caller that forgot to resolve a vendor never composes an ambiguous key.
// Returns "" when the kind is unknown or the workload is empty (isolation: keys
// are always namespaced by workload).
func artifactPrefix(ref *nodev1.ArtifactRef, vendor string) string {
	kindStr := artifactKindStr(ref.GetKind())
	if kindStr == "" || ref.GetWorkload() == "" {
		return ""
	}
	if artifactVendorSegment(ref.GetKind()) {
		if vendor == "" {
			return ""
		}
		if r := ref.GetRef(); r != "" {
			return kindStr + "/" + vendor + "/" + ref.GetWorkload() + "/" + r
		}
		return kindStr + "/" + vendor + "/" + ref.GetWorkload()
	}
	if r := ref.GetRef(); r != "" {
		return kindStr + "/" + ref.GetWorkload() + "/" + r
	}
	return kindStr + "/" + ref.GetWorkload()
}

// The binding sidecars record, on disk beside a banked bundle, the control-plane
// identity noded needs to compose the bundle's REMOTE (S3) prefix at eviction
// time (#38 F1/F2). The bundle dir name is only the opaque snapshot_ref, so
// without these a boot-scanned inventory entry seeds with an empty workload /
// group_instance_id and its remote evict fails InvalidArgument, stranding the S3
// copy. Written by the server AFTER the driver publishes the (already complete)
// bundle, mirroring the driver's gen/pinned-IP sidecars; a bundle banked before
// this change (or a crash between snapfile-publish and sidecar-write) simply has
// no sidecar and reads back empty, which the reaper then SKIPS (see #38 fix C).
const (
	statefulWorkloadSidecar = "workload"
	groupInstanceSidecar    = "group_instance_id"
)

// writeStatefulWorkloadSidecar records the workload beside a banked stateful
// bundle (stateful/<ref>/workload). Best-effort: a write failure logs and leaves
// the bundle usable, degrading only to the empty-binding SKIP the reaper handles.
func (s *Server) writeStatefulWorkloadSidecar(ref, workload string) {
	if s.statefulDriver == nil || workload == "" || ref == "" {
		return
	}
	root := s.statefulDriver.StatefulDir()
	if root == "" {
		return
	}
	path := filepath.Join(root, ref, statefulWorkloadSidecar)
	if err := os.WriteFile(path, []byte(workload), 0o600); err != nil {
		s.logger.Warn("noded: write stateful workload sidecar (bundle still usable, will seed empty on restart)", "ref", ref, "workload", workload, "err", err)
	}
}

// readStatefulWorkloadSidecar reads the workload a stateful bundle was banked for
// ("" if the sidecar is absent or unreadable: a pre-sidecar bundle).
func (s *Server) readStatefulWorkloadSidecar(ref string) string {
	if s.statefulDriver == nil || ref == "" {
		return ""
	}
	root := s.statefulDriver.StatefulDir()
	if root == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(root, ref, statefulWorkloadSidecar))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// writeGroupInstanceSidecar records the group_instance_id beside a banked group
// member bundle (group/<set>/<member>/group_instance_id). Best-effort, same
// contract as the stateful sidecar.
func (s *Server) writeGroupInstanceSidecar(setID, memberName, groupInstanceID string) {
	if s.groupDriver == nil || groupInstanceID == "" || setID == "" || memberName == "" {
		return
	}
	root := s.groupDriver.GroupSetsDir()
	if root == "" {
		return
	}
	path := filepath.Join(root, setID, memberName, groupInstanceSidecar)
	if err := os.WriteFile(path, []byte(groupInstanceID), 0o600); err != nil {
		s.logger.Warn("noded: write group instance sidecar (bundle still usable, will seed empty on restart)", "set", setID, "member", memberName, "gid", groupInstanceID, "err", err)
	}
}

// readGroupInstanceSidecar reads the group_instance_id a member bundle was banked
// under ("" if absent: a pre-sidecar bundle).
func (s *Server) readGroupInstanceSidecar(setID, memberName string) string {
	if s.groupDriver == nil || setID == "" || memberName == "" {
		return ""
	}
	root := s.groupDriver.GroupSetsDir()
	if root == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(root, setID, memberName, groupInstanceSidecar))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// legacyArtifactPrefix composes the pre-R7 un-vendored prefix for a vendor-bound
// kind (<kindStr>/<workload>/<ref>), the key layout every BASE/SESSION/SERVING/
// STATEFUL/GROUP_SET artifact used before vendor keying shipped. It is the alias
// target: an artifact found here is treated as vendor "amd" (the node-4 alias,
// standing decision 11) without re-exporting. Returns "" for VOLUME (which never
// had a different layout to alias from) or an unknown kind/empty workload.
func legacyArtifactPrefix(ref *nodev1.ArtifactRef) string {
	if !artifactVendorSegment(ref.GetKind()) {
		return ""
	}
	kindStr := artifactKindStr(ref.GetKind())
	if kindStr == "" || ref.GetWorkload() == "" {
		return ""
	}
	if r := ref.GetRef(); r != "" {
		return kindStr + "/" + ref.GetWorkload() + "/" + r
	}
	return kindStr + "/" + ref.GetWorkload()
}

// artifactLocalDir resolves the on-disk directory holding a ref's files. It
// mirrors the driver's per-kind bundle layout: BASES are node-shared under
// SnapshotRoot/bases, while the WARMTH kinds (sessions/, serving/, stateful/,
// group/<set_id>/) live under WarmthRoot, which for a brick nests at
// SnapshotRoot/instances/<pod_uid> and for the legacy DaemonSet equals
// SnapshotRoot (the driver's warmthRoot fallback is mirrored here). Volumes live
// under the volume manager's VolumeRoot/<workload>. Returns "" when the kind is
// unknown or the relevant substrate is not configured, which the caller maps to
// FAILED_PRECONDITION.
func (s *Server) artifactLocalDir(ref *nodev1.ArtifactRef) string {
	root := s.cfg.SnapshotRoot
	// Warmth root, mirroring driver.warmthRoot: WarmthRoot when set, else
	// SnapshotRoot (a Config that never derived WarmthRoot keeps the flat layout).
	warmth := s.cfg.WarmthRoot
	if warmth == "" {
		warmth = s.cfg.SnapshotRoot
	}
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_BASE:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "bases", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION:
		if warmth == "" {
			return ""
		}
		return filepath.Join(warmth, "sessions", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_SERVING:
		if warmth == "" {
			return ""
		}
		return filepath.Join(warmth, "serving", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		if warmth == "" {
			return ""
		}
		return filepath.Join(warmth, "stateful", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET:
		if warmth == "" {
			return ""
		}
		return filepath.Join(warmth, "group", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME:
		if s.volumes == nil {
			return ""
		}
		// The volume manager owns VolumeRoot/<workload> (vol.img + gen). VolumePath
		// names the vol.img inside it; its parent is the artifact dir.
		return filepath.Dir(s.volumes.VolumePath(ref.GetWorkload()))
	default:
		return ""
	}
}

// enumerateArtifactFiles lists the files (relative to localDir) that make up an
// artifact: every regular file under the dir except in-progress *.tmp temps and
// the meta.json marker the store itself writes. A bundle dir holds exactly one
// bundle's files (snapfile + memfile + any sidecars), and a group SET dir holds
// its members' files under per-member subdirs, so a recursive walk yields the
// complete file list with slash-relative names the store maps 1:1 to keys. An
// empty result (missing dir, or no files) means the artifact is absent locally.
func enumerateArtifactFiles(localDir string) ([]string, error) {
	info, err := os.Stat(localDir)
	if err != nil || !info.IsDir() {
		return nil, err
	}
	var files []string
	walkErr := filepath.WalkDir(localDir, func(path string, d os.DirEntry, werr error) error {
		if werr != nil {
			return fmt.Errorf("walk %q: %w", path, werr)
		}
		if d.IsDir() {
			return nil
		}
		name := d.Name()
		if strings.HasSuffix(name, ".tmp") || name == "meta.json" {
			return nil
		}
		rel, rerr := filepath.Rel(localDir, path)
		if rerr != nil {
			return rerr
		}
		// Store keys use forward slashes on every platform; filepath.Rel returns
		// OS-native separators, so normalise (a no-op on linux).
		files = append(files, filepath.ToSlash(rel))
		return nil
	})
	if walkErr != nil {
		return nil, walkErr
	}
	return files, nil
}

// StartStoreLoops starts the R6 background machinery: the bounded async
// export-worker pool, the periodic store-reachability probe, and a one-shot
// reconcile sweep that enqueues an export for any local artifact whose store
// copy is missing or stale. All three no-op when the store is disabled (nil).
// The export queue is fire-and-forget, so none of this ever holds the bank path
// or the drain deadline. Called once from the daemon entrypoint after the
// startup reconcile sequence; ctx cancels every loop on shutdown.
func (s *Server) StartStoreLoops(ctx context.Context) {
	s.startExportQueue(ctx)
	s.startStoreProbe(ctx)
	s.enqueueReconcileExports(ctx)
}

// ---- Continuity verbs (R6) -------------------------------------------------

// ExportArtifact copies one banked, crash-consistent artifact off node to the
// object store under its Fork-3 prefix (files first, meta.json LAST). It is
// idempotent per checksum: an unchanged artifact returns skipped=true with
// bytes_moved=0. FAILED_PRECONDITION when the store is disabled (nil) or the
// local artifact is absent. It NEVER touches a live VM: the ref names a down,
// banked artifact (post-bank-commit), so enumeration reads only its on-disk
// files.
//
// A BASE artifact is exported ASYNCHRONOUSLY (fast-durability-export fix): a large
// base memfile is up to a few GB, and streaming it to SeaweedFS inside this RPC
// held the control-plane -> noded gRPC connection open for minutes with no wire
// activity (noded busy io.Copy-ing to the store). Neither side sets gRPC keepalive
// (the noded server sets no keepalive.ServerParameters, so its MaxConnectionIdle /
// MaxConnectionAge are infinite, and the control-plane Mint client sends no
// keepalive pings), so a long in-progress call with zero server->client frames is
// eventually reaped by an on-path L4 idle timeout (Cilium is eBPF L4 with no L7
// proxy in this path, so a stale conntrack/NAT entry for a no-packet TCP flow is
// the closer, not a mesh proxy) and the CP saw {:error, "the connection is closed"}
// mid-upload. Every SMALL base (memfile <=768MB) landed; the LARGE ones (bazel-query
// ~3G, scratch-k8s/sandbox-session ~2G, semgrep ~1.5G) never did. Rather than keep a
// multi-minute synchronous call alive with client keepalive, a BASE export is
// enqueued onto the existing bounded async export queue (scoped to EXACTLY this ref,
// never the blanket enqueueReconcileExports sweep, which must not ship the ~245
// leaked base versions into the store) and the RPC returns a fast ack. The control
// plane confirms completion by reading the additive exported flag on WorkloadCapacity
// and re-issues on its 60s reconcile if a queued export was dropped or lost. Every
// other (SMALL) kind keeps the synchronous path.
func (s *Server) ExportArtifact(ctx context.Context, req *nodev1.ExportArtifactRequest) (*nodev1.ExportArtifactResponse, error) {
	if s.store == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: object store not configured; export unavailable")
	}
	ref := req.GetArtifact()
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	if prefix == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: artifact kind and workload required")
	}
	localDir := s.artifactLocalDir(ref)
	if localDir == "" {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: artifact kind %s not exportable on this node", ref.GetKind())
	}
	files, err := enumerateArtifactFiles(localDir)
	if err != nil || len(files) == 0 {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: local artifact %q absent or empty (nothing to export)", prefix)
	}
	// BASE: hand the (validated, present) ref to the async queue and ack fast, so a
	// multi-minute upload never holds the meshed RPC open past a proxy idle timeout.
	// The queue is started in StartStoreLoops; when it is not running (a Server built
	// without the loops, e.g. a test), fall through to the synchronous path so the
	// export still happens rather than being silently dropped.
	if ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_BASE && s.exportCh != nil {
		s.enqueueExport(ref)
		return &nodev1.ExportArtifactResponse{BytesMoved: 0, Skipped: false, Generation: 0}, nil
	}
	generation := s.artifactGeneration(ref)
	moved, skipped, err := s.store.Export(ctx, prefix, localDir, files, generation, time.Now().UnixMilli(), s.cfg.CpuVendor, s.cfg.CpuTemplate)
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "noded: export artifact %q: %v", prefix, err)
	}
	s.exported.mark(prefix, generation)
	s.signalChange()
	return &nodev1.ExportArtifactResponse{BytesMoved: uint64(moved), Skipped: skipped, Generation: generation}, nil
}

// RestoreArtifact fetches an artifact from the store back onto local disk into
// the correct per-kind dir, verifying every file's checksum, then re-registers
// it via the same reconcile helpers a rescan uses so a later wake sees it.
// Idempotent: an artifact already present locally with a matching checksum is a
// skipped no-op. FAILED_PRECONDITION when the store is disabled, or the store
// copy is absent/incomplete/mismatched.
//
// BASE restores are ASYNC (base-durability PR-2): a base is a multi-GB S3
// download, and holding this RPC open for the minutes it takes lets the
// Cilium/eBPF datapath reap the idle flow's conntrack entry mid-transfer ("the
// connection is closed"), the same failure that forced base EXPORT async. So a
// BASE restore that genuinely needs a download runs the cheap presence + sku +
// already-local checks SYNCHRONOUSLY (so a store-miss is a fast, distinguishable
// FAILED_PRECONDITION the caller falls back to rebuild on, and an already-local
// base is an inline skipped no-op), then ENQUEUES the download onto a bounded,
// deduped queue and fast-ACKs accepted=true. The caller polls NodeStatus for the
// base to appear READY. Every other (small) kind still restores inline.
func (s *Server) RestoreArtifact(ctx context.Context, req *nodev1.RestoreArtifactRequest) (*nodev1.RestoreArtifactResponse, error) {
	if s.store == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: object store not configured; restore unavailable")
	}
	ref := req.GetArtifact()
	prefix, err := s.resolveRestorePrefix(ctx, ref, req.GetVendor())
	if err != nil {
		return nil, err
	}
	localDir := s.artifactLocalDir(ref)
	if localDir == "" {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: artifact kind %s not restorable on this node", ref.GetKind())
	}
	// Sku gate (PR-E, grandfather rule): read the stamped meta.json BEFORE moving
	// any bytes and refuse a PRESENT-BUT-MISMATCHED cpu_sku loudly. A stamp
	// missing entirely (both fields "") is a legacy/UNSTAMPED artifact and is
	// NEVER refused here (grandfathered-compatible); refusing it would be data
	// loss, exactly the failure mode this rule exists to prevent. A present
	// stamp that matches this node's own sku, or that the node cannot judge
	// (its own vendor/template undetected), also passes.
	present, gen, stampedVendor, stampedTemplate, perr := s.store.Present(ctx, prefix)
	if perr == nil && present {
		if mismatch, got, want := cpuSkuMismatch(stampedVendor, stampedTemplate, s.cfg.CpuVendor, s.cfg.CpuTemplate); mismatch {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: cpu_sku mismatch on restore: artifact stamped %q != node %q", got, want)
		}
	}
	// Idempotency: if the artifact is already present locally with a checksum
	// matching the store's marker, the restore is a no-op (the store Export's own
	// same-checksum compare is the authority; re-check presence cheaply first).
	if local, err := enumerateArtifactFiles(localDir); err == nil && len(local) > 0 {
		if perr == nil && present {
			// A local copy exists; treat as already-restored (skipped). The
			// content-level equality lives in Export's checksum compare on the next
			// export; a restore's job is to make local non-empty, which it is.
			s.reregisterRestored(ref)
			return &nodev1.RestoreArtifactResponse{Skipped: true, Generation: gen}, nil
		}
	}
	// A genuine download is needed. The store copy must be present to attempt it:
	// a not-present copy is FAILED_PRECONDITION (fast, distinguishable), so the
	// caller falls back to rebuild AT ONCE rather than waiting a timeout for a
	// base that is not in S3. A transport error probing presence is also
	// FAILED_PRECONDITION (we cannot prove the copy exists; do not enqueue a
	// doomed download). This gate is shared by both the async BASE path and the
	// inline small-kind path.
	if perr != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restore artifact %q: store presence probe failed: %v", prefix, perr)
	}
	if !present {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restore artifact %q: not present in store", prefix)
	}

	// BASE: fast-ACK and download asynchronously (multi-GB; must not hold the RPC).
	if ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_BASE {
		s.enqueueRestore(ref, prefix, localDir)
		return &nodev1.RestoreArtifactResponse{Accepted: true}, nil
	}

	// Every other (small) kind restores inline: the download is quick enough that
	// the idle-flow-reap risk does not apply, and the caller's existing inline
	// restore-on-miss semantics are unchanged.
	moved, generation, err := s.store.Restore(ctx, prefix, localDir)
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restore artifact %q: %v", prefix, err)
	}
	s.reregisterRestored(ref)
	s.exported.mark(prefix, generation)
	s.signalChange()
	return &nodev1.RestoreArtifactResponse{BytesMoved: uint64(moved), Generation: generation}, nil
}

// resolveRestorePrefix composes the store prefix a restore reads from. It
// requires a vendor for a vendor-bound kind (FAILED_PRECONDITION when the
// caller left it and the ref's kind needs one, VOLUME excepted). vendor
// mismatches the node's own reported vendor are refused closed here too: the
// daemon restores only the vendor-matching copy and never silently substitutes
// a cold boot at this layer (the CP plans that). One exception: when the
// vendor-keyed prefix holds no artifact but the pre-R7 un-vendored legacy
// prefix does, AND the requested vendor is the node-4 alias ("amd", standing
// decision 11), the legacy prefix is used directly rather than treated as
// absent, so an existing pre-vendor-keying artifact is never needlessly
// re-exported under the new layout.
func (s *Server) resolveRestorePrefix(ctx context.Context, ref *nodev1.ArtifactRef, vendor string) (string, error) {
	if artifactVendorSegment(ref.GetKind()) {
		if vendor == "" {
			return "", status.Error(codes.InvalidArgument, "noded: vendor required to restore this artifact kind")
		}
		if s.cfg.CpuVendor != "" && vendor != s.cfg.CpuVendor {
			return "", status.Errorf(codes.FailedPrecondition, "noded: vendor mismatch on restore: requested %q != node %q", vendor, s.cfg.CpuVendor)
		}
	}
	prefix := artifactPrefix(ref, vendor)
	if prefix == "" {
		return "", status.Error(codes.InvalidArgument, "noded: artifact kind and workload required")
	}
	if legacy := legacyArtifactPrefix(ref); legacy != "" && vendor == legacyVendorAlias {
		if present, _, _, _, err := s.store.Present(ctx, prefix); err == nil && !present {
			if legacyPresent, _, _, _, lerr := s.store.Present(ctx, legacy); lerr == nil && legacyPresent {
				return legacy, nil
			}
		}
	}
	return prefix, nil
}

// EvictArtifact deletes an artifact. remote=true evicts the store copy
// (meta.json first, so a partial delete is invisible); remote=false evicts the
// LOCAL copy over the typed ref via the existing EvictSnapshot/RemoveBundle
// path. A VOLUME (remote OR local) is refused FAILED_PRECONDITION while its
// generation still pairs with a local bundle (standing decision 8: the store is
// never the only copy of a generation a banked bundle needs). Idempotent on an
// already-absent artifact.
func (s *Server) EvictArtifact(ctx context.Context, req *nodev1.EvictArtifactRequest) (*nodev1.EvictArtifactResponse, error) {
	ref := req.GetArtifact()
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	if prefix == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: artifact kind and workload required")
	}
	// Volume pairing guard: refuse to evict a volume whose generation still pairs
	// with a banked local bundle (mirrors the DeleteVolume attach guard's intent
	// for the store copy). Applied to both remote and local volume evictions.
	if ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME {
		if s.volumeGenerationStillPaired(ref.GetWorkload()) {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: volume for %q still pairs with a banked bundle; refusing evict", ref.GetWorkload())
		}
	}
	// Stateful in-use guard (defense-in-depth, #38): refuse to evict a STATEFUL
	// artifact (local OR remote store copy) while a live VM was relit from this ref.
	// The reaper only ever targets orphans (no live instance) and gates its remote
	// evict on the local one, so in normal operation this never fires; it is the
	// backstop against a mistargeted control-plane request deleting the recovery
	// copy of a bundle a running guest still depends on, mirroring the volume
	// pairing guard's intent for the store copy.
	if ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL && s.statefulVMs != nil {
		if vmID, inUse := s.statefulVMs.snapshotRefInUse(ref.GetRef()); inUse {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful snapshot %q is in use by live vm %q; refusing evict", ref.GetRef(), vmID)
		}
	}
	if req.GetRemote() {
		if s.store == nil {
			// No store: the remote copy cannot exist, so the desired end-state
			// (gone) already holds. Idempotent success.
			return &nodev1.EvictArtifactResponse{}, nil
		}
		if err := s.store.DeleteArtifact(ctx, prefix); err != nil {
			return nil, status.Errorf(codes.Unavailable, "noded: evict remote artifact %q: %v", prefix, err)
		}
		s.exported.clear(prefix)
		s.signalChange()
		return &nodev1.EvictArtifactResponse{}, nil
	}
	// Local eviction over the typed ref: reuse the existing EvictSnapshot path for
	// bundle kinds, DeleteVolume for a VOLUME.
	return s.evictArtifactLocal(ctx, ref)
}

// evictArtifactLocal deletes the on-disk copy of an artifact over the typed ref,
// reusing the kind-specific eviction path (EvictSnapshot for a session/serving
// bundle, the stateful arm for a stateful bundle, RemoveGroupMemberBundle per
// member for a group set, DeleteVolume for a volume, a direct bases/<ref> removal
// for a BASE). Idempotent.
func (s *Server) evictArtifactLocal(ctx context.Context, ref *nodev1.ArtifactRef) (*nodev1.EvictArtifactResponse, error) {
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_BASE:
		return s.evictBaseLocal(ref)
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, nodev1.ArtifactKind_ARTIFACT_KIND_SERVING:
		if _, err := s.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref.GetRef()}); err != nil {
			return nil, err
		}
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		// Dispatch STATEFUL directly to its own arm (NOT via the generic
		// EvictSnapshot inventory dispatch) so a typed stateful evict removes the
		// on-disk stateful/<ref> dir even if the banked-bundle inventory entry is
		// missing (e.g. a bundle that reconcile has not re-registered). The arm's
		// RemoveStatefulBundle is idempotent, so an already-absent bundle is
		// success; its in-use guard still refuses a bundle a live relit VM holds.
		if _, err := s.evictStatefulSnapshot(ref.GetRef()); err != nil {
			return nil, err
		}
	case nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME:
		if _, err := s.DeleteVolume(ctx, &nodev1.DeleteVolumeRequest{Workload: ref.GetWorkload()}); err != nil {
			return nil, err
		}
	case nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET:
		// A group set's LOCAL eviction is per-member (the control plane evicts a
		// set by evicting each member ref via EvictSnapshot, as R5 established);
		// there is no single set-level local-evict driver op, so the typed local
		// evict for a set is Unimplemented and the CP uses the per-member path.
		// The REMOTE evict (handled above, before this call) does delete the whole
		// set prefix at once, which is why a set is still one ArtifactRef.
		return nil, status.Error(codes.Unimplemented, "noded: group set local eviction is per-member (evict each member ref)")
	default:
		return nil, status.Errorf(codes.InvalidArgument, "noded: artifact kind %s not locally evictable", ref.GetKind())
	}
	return &nodev1.EvictArtifactResponse{}, nil
}

// evictBaseLocal removes a base snapshot dir (SnapshotRoot/bases/<ref>) behind
// two safety guards, then forgets any registry entry so NodeStatus stops
// advertising it. It is the local arm the control plane's superseded-ref eviction
// and its reconciled retention sweep drive (PR-3): before this arm existed, a BASE
// local evict fell through to InvalidArgument, so the control plane's EvictSnapshot
// dispatched a base ref into the SESSION bundle path (RemoveSessionBundle), which
// no-op-removed a nonexistent sessions/<ref> dir and returned success while
// bases/<ref> survived. Every superseded base leaked that way.
//
// Guards (both refuse FAILED_PRECONDITION; these are the REAL safety, and they are
// the only two facts noded can judge authoritatively about a ref):
//   - (a) IN-USE: no live task VM (or pre-adoption session VM) was restored from
//     this ref (vmRegistry.snapshotRefInUse). Evicting a base out from under a
//     running guest that restored from it would strand the birth lineage.
//   - (b) BUILDING: the ref is not currently BUILDING (a build writes into
//     bases/<ref>; removing it mid-build corrupts the in-progress snapshot).
//
// Deliberately NO registry-membership, not-last, or "current base" guard here.
// noded does not know the control plane's authoritative CURRENT ref (its own
// per-workload capacity projection is last-wins-nondeterministic among multiple
// provisioned READY bases, so it cannot be trusted to identify current), and a
// registry/completeness guard would REFUSE exactly the artifacts PR-3 must delete:
// the pre-R2 superseded versions AND the incomplete/.tmp orphan dirs (a build that
// died mid-write leaves memfile.tmp/snapfile.tmp with no snapfile, so
// ReconcileBasesFromDisk never registers it, so a registry-gated guard would strand
// it forever). CURRENT-base protection is the CONTROL PLANE's job and is enforced
// there: the retention sweep's desired set always includes the workload's current
// ref (never a candidate) and the ongoing superseded-eviction path only ever names
// a drained superseded ref, so noded is never asked to evict a current base. The
// in-use and BUILDING guards below are the defense-in-depth backstop against a
// mistargeted request. Result: every non-current, non-in-use, non-BUILDING dir
// (registered, unregistered, or a .tmp orphan) is evictable, which is what drains
// the backlog to exactly the current set.
//
// Idempotent: an already-absent dir is success (the desired end-state holds), so a
// retry or a re-sweep is harmless.
func (s *Server) evictBaseLocal(ref *nodev1.ArtifactRef) (*nodev1.EvictArtifactResponse, error) {
	baseRef := ref.GetRef()
	if baseRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: base ref required for local eviction")
	}
	if s.cfg.SnapshotRoot == "" {
		return nil, status.Error(codes.FailedPrecondition, "noded: snapshot root not configured; base not locally evictable")
	}
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", baseRef)

	// (a) In-use guard: refuse while a live VM was restored from this base ref.
	if vmID, inUse := s.vms.snapshotRefInUse(baseRef); inUse {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: base %q is in use by live vm %q; refusing evict", baseRef, vmID)
	}

	// (b) BUILDING guard: never remove a base dir a BuildBase is writing into. An
	// unknown ref (no registry entry, e.g. a superseded or .tmp-orphan dir) is by
	// definition not BUILDING, so this only ever fires for a live in-progress build.
	if entry, known := s.bases.get(baseRef); known && entry.state == nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: base %q is BUILDING; refusing evict", baseRef)
	}

	// Remove the on-disk dir (idempotent: RemoveAll on an absent path is nil) and
	// forget any registry entry so NodeStatus stops advertising it.
	if err := os.RemoveAll(dir); err != nil {
		return nil, status.Errorf(codes.Internal, "noded: evict base %q: %v", baseRef, err)
	}
	s.bases.remove(baseRef)
	s.signalChange()
	return &nodev1.EvictArtifactResponse{}, nil
}

// artifactGeneration reports the generation to stamp into an artifact's export
// marker: the volume's current generation for a VOLUME (the pairing fact), the
// stamped bundle generation for a STATEFUL bundle, 0 for kinds without one.
func (s *Server) artifactGeneration(ref *nodev1.ArtifactRef) uint64 {
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME:
		if s.volumes == nil {
			return 0
		}
		gen, err := s.volumes.Generation(ref.GetWorkload())
		if err != nil {
			return 0
		}
		return gen
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		if b, ok := s.statefulBundles.get(ref.GetRef()); ok {
			return b.generation
		}
		return 0
	default:
		return 0
	}
}

// volumeGenerationStillPaired reports whether a workload's CURRENT volume
// generation still equals a banked local stateful bundle's stamped generation.
// While it does, evicting the volume (locally or remotely) would strand a bundle
// that can only relight against that generation, so EvictArtifact refuses it
// (standing decision 8).
func (s *Server) volumeGenerationStillPaired(workload string) bool {
	if s.volumes == nil || s.statefulBundles == nil {
		return false
	}
	volGen, err := s.volumes.Generation(workload)
	if err != nil {
		return false
	}
	if b, ok := s.statefulBundles.byWorkload(workload); ok && b.generation == volGen {
		return true
	}
	return false
}

// reregisterRestored re-seeds the in-memory inventory for a just-restored
// artifact by re-running the kind's disk reconcile helper, so a rescan/adoption
// (and the NodeStatus projection) sees it immediately, exactly as if the daemon
// had found it on a startup scan. A VOLUME needs no re-registration (volume.Scan
// reads VolumeRoot fresh on every NodeStatus).
func (s *Server) reregisterRestored(ref *nodev1.ArtifactRef) {
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_BASE:
		s.ReconcileBasesFromDisk()
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION:
		s.ReconcileSessionsFromDisk()
	case nodev1.ArtifactKind_ARTIFACT_KIND_SERVING:
		s.ReconcileServingFromDisk()
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		s.ReconcileStatefulFromDisk()
	case nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET:
		s.ReconcileGroupBundlesFromDisk()
	case nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME:
		// No in-memory seeding: volume.Scan reads disk truth on every NodeStatus.
	}
}

// ---- async export-after-commit queue ---------------------------------------

// exportQueueWorkers is the bounded worker pool draining the export queue. Two
// is enough to overlap a large volume export with a bundle export without
// letting exports contend with the bank path (they are fire-and-forget).
const exportQueueWorkers = 2

// exportQueueDepth bounds the buffered enqueue channel. An enqueue that would
// block (queue full) is DROPPED, not awaited: the export queue must never stall
// the bank path or the drain deadline (standing decision 7), and the next
// reconcile re-enqueues any artifact whose store copy is still missing.
const exportQueueDepth = 256

// exportJob names one artifact to export, carrying enough to compose its prefix
// and local dir. It is keyed (in the dedupe set) by its store prefix so a
// re-enqueue of an already-queued artifact is dropped.
type exportJob struct {
	ref *nodev1.ArtifactRef
	key string // the store prefix, the dedupe key
}

// startExportQueue launches the bounded export- AND restore-worker pools. It is
// idempotent and a no-op when the store is disabled (nil): with no store there is
// nothing to export or restore. Called once from the daemon entrypoint after the
// server is built. The workers run until ctx is cancelled (daemon shutdown),
// draining fire-and-forget. The restore queue shares this lifecycle (one
// sync.Once) so both pools start and stop together.
func (s *Server) startExportQueue(ctx context.Context) {
	if s.store == nil {
		return
	}
	s.exportOnce.Do(func() {
		s.exportCh = make(chan exportJob, exportQueueDepth)
		for i := 0; i < exportQueueWorkers; i++ {
			go s.exportWorker(ctx)
		}
		s.restoreCh = make(chan restoreJob, restoreQueueDepth)
		for i := 0; i < restoreQueueWorkers; i++ {
			go s.restoreWorker(ctx)
		}
	})
}

// enqueueExport schedules an artifact for async write-back. It is non-blocking:
// a full queue or an already-queued key (same prefix) drops the enqueue silently
// (the next reconcile re-enqueues a still-missing copy). It no-ops when the store
// is disabled or the queue is not started. It NEVER blocks the caller (the bank
// path / drain deadline), by design.
func (s *Server) enqueueExport(ref *nodev1.ArtifactRef) {
	if s.store == nil || s.exportCh == nil {
		return
	}
	key := artifactPrefix(ref, s.cfg.CpuVendor)
	if key == "" {
		return
	}
	s.exportDedupeMu.Lock()
	if _, queued := s.exportDedupe[key]; queued {
		s.exportDedupeMu.Unlock()
		return // already queued; a re-enqueue is a no-op
	}
	s.exportDedupe[key] = struct{}{}
	s.exportDedupeMu.Unlock()

	select {
	case s.exportCh <- exportJob{ref: ref, key: key}:
	default:
		// Queue full: drop and un-mark so a later reconcile can re-enqueue. The
		// artifact stays durable on local disk; this only delays the off-node copy.
		s.exportDedupeMu.Lock()
		delete(s.exportDedupe, key)
		s.exportDedupeMu.Unlock()
		s.logger.Warn("noded: export queue full; dropping enqueue (will retry on reconcile)", "artifact", key)
	}
}

// exportWorker drains the export queue, running each export fire-and-forget. An
// export failure is logged, never retried inline (the next reconcile re-enqueues
// a still-missing copy); a success marks the artifact exported so NodeStatus
// reflects it. It exits when ctx is done or the channel is closed.
func (s *Server) exportWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case job, ok := <-s.exportCh:
			if !ok {
				return
			}
			s.runExportJob(ctx, job)
		}
	}
}

// runExportJob performs one queued export. For a VOLUME it short-circuits when
// the current generation is already exported (the store's own Head-compare would
// also skip, but this avoids the round-trip). It always clears the dedupe key so
// a subsequent change can re-enqueue.
//
// No OpenTelemetry span is emitted here (R6, Task 11): noded has no Go otel tracer
// wired (unlike the control plane), and inventing a tracing dependency for one span
// is out of scope. Export visibility comes from the structured logs below
// ("noded: exported artifact off node" / "noded: async export failed") and the
// export-backlog alert keys on the "export queue full" log. The control-plane
// `embervm.artifact_restore` span covers the paired read path.
func (s *Server) runExportJob(ctx context.Context, job exportJob) {
	defer func() {
		s.exportDedupeMu.Lock()
		delete(s.exportDedupe, job.key)
		s.exportDedupeMu.Unlock()
	}()

	generation := s.artifactGeneration(job.ref)
	// Local short-circuit for a VOLUME whose current generation is already the
	// exported one (standing decision 6: skip a re-export when gen is unchanged).
	if job.ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME {
		if lastGen, ok := s.exported.generation(job.key); ok && lastGen == generation {
			return
		}
	}
	localDir := s.artifactLocalDir(job.ref)
	if localDir == "" {
		return
	}
	files, err := enumerateArtifactFiles(localDir)
	if err != nil || len(files) == 0 {
		// The artifact vanished (evicted before the export ran); nothing to do.
		return
	}
	_, skipped, err := s.store.Export(ctx, job.key, localDir, files, generation, time.Now().UnixMilli(), s.cfg.CpuVendor, s.cfg.CpuTemplate)
	if err != nil {
		s.logger.Warn("noded: async export failed (will retry on reconcile)", "artifact", job.key, "err", err)
		return
	}
	s.exported.mark(job.key, generation)
	if !skipped {
		s.logger.Info("noded: exported artifact off node", "artifact", job.key, "generation", generation)
	}
	s.signalChange()
}

// enqueueReconcileExports sweeps the local banked inventory on startup and
// enqueues an export for any artifact whose store copy is missing or stale
// (covers "a roll exited before exports finished", standing decision 7). It runs
// off the request path (a goroutine) so a slow store never delays startup, and
// checks presence per artifact so an already-durable one is not re-uploaded. It
// no-ops when the store is disabled.
func (s *Server) enqueueReconcileExports(ctx context.Context) {
	if s.store == nil {
		return
	}
	go func() {
		// Session bundles.
		for _, e := range s.sessionSnap.snapshot() {
			s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: e.workload, Ref: e.snapshotRef})
		}
		// Serving bundles.
		for _, e := range s.servingSnap.snapshot() {
			s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: e.workload, Ref: e.snapshotRef})
		}
		// Stateful bundles + their paired volumes.
		for _, e := range s.statefulBundles.snapshot() {
			s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: e.workload, Ref: e.snapshotRef})
		}
		if s.volumes != nil {
			if inv, err := s.volumes.Scan(); err == nil {
				for _, v := range inv {
					s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: v.Workload})
				}
			}
		}
		// Group bundle sets: enqueue the SET (keyed by set_id) once.
		seenSets := make(map[string]struct{})
		for _, e := range s.groupBundles.snapshot() {
			if _, ok := seenSets[e.setID]; ok {
				continue
			}
			seenSets[e.setID] = struct{}{}
			s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET, Workload: e.groupInstanceID, Ref: e.setID})
		}
	}()
}

// enqueueIfMissing enqueues an export only when the store copy is absent or,
// for a VOLUME, its generation lags the current one. A store error is treated as
// "enqueue anyway" (fail toward durability): the export's own Head-compare then
// makes the final skip decision.
func (s *Server) enqueueIfMissing(ctx context.Context, ref *nodev1.ArtifactRef) {
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	if prefix == "" {
		return
	}
	// A vendor-bound kind already durable under the legacy un-vendored prefix
	// (this node's own artifact, exported before vendor keying shipped) is
	// already exported; treat it as present so the reconcile sweep never
	// re-exports it under the new layout for content it already has off node.
	// Gated on the node itself being the alias vendor: the legacy layout can
	// only ever hold node-4 (amd) artifacts, so a node of any other vendor must
	// never claim a legacy copy as its own durable export (it would silently
	// skip exporting its own artifact).
	if legacy := legacyArtifactPrefix(ref); legacy != "" && s.cfg.CpuVendor == legacyVendorAlias {
		if legacyPresent, legacyGen, _, _, lerr := s.store.Present(ctx, legacy); lerr == nil && legacyPresent {
			s.exported.mark(prefix, legacyGen)
			return
		}
	}
	present, gen, _, _, err := s.store.Present(ctx, prefix)
	if err == nil && present {
		if ref.GetKind() == nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME {
			if cur := s.artifactGeneration(ref); cur == gen {
				s.exported.mark(prefix, gen) // already durable at the current gen
				return
			}
		} else {
			s.exported.mark(prefix, gen)
			return
		}
	}
	s.enqueueExport(ref)
}

// ---- async BASE-restore queue ----------------------------------------------

// restoreQueueWorkers is the bounded worker pool draining the restore queue. Two
// overlaps a large base download with a second without letting downloads pile
// unbounded. A base restore is demand-driven (the CP triggers one when a base is
// needed and missing), so contention is naturally low; two is a comfortable
// ceiling that still overlaps a fresh-node multi-base hydrate.
const restoreQueueWorkers = 2

// restoreQueueDepth bounds the buffered restore-enqueue channel. An enqueue that
// would block (queue full) is DROPPED, not awaited: the RPC has already fast-ACKed
// accepted=true, so a dropped enqueue simply means the base does not appear READY
// and the CP re-triggers (or falls back to rebuild) on its poll timeout. It is
// generous because the dedupe set (held enqueue-through-completion) already caps
// distinct in-flight restores to one per prefix.
const restoreQueueDepth = 64

// restoreJob names one BASE artifact to download, carrying its resolved store
// prefix (already vendor/legacy-resolved by resolveRestorePrefix) and local dir
// so the worker needs no further resolution. Keyed (in the dedupe set) by prefix
// so a re-triggered restore of an in-flight base is dropped.
type restoreJob struct {
	ref      *nodev1.ArtifactRef
	prefix   string // resolved store prefix, the dedupe key
	localDir string
}

// enqueueRestore schedules a BASE download for async write-back. It is
// non-blocking: a full queue drops the enqueue (the CP's poll re-triggers or
// falls back to rebuild), and an already-in-flight prefix is a no-op. The dedupe
// key is held enqueue-THROUGH-COMPLETION (cleared in runRestoreJob's defer, not on
// dequeue), so a re-triggered restore of a base still downloading never starts a
// second concurrent download of the same prefix (the node's queue is the real
// dedupe guard for the CP's retriggers). It no-ops when the store is disabled or
// the queue is not started.
func (s *Server) enqueueRestore(ref *nodev1.ArtifactRef, prefix, localDir string) {
	if s.store == nil || s.restoreCh == nil || prefix == "" {
		return
	}
	s.restoreDedupeMu.Lock()
	if _, queued := s.restoreDedupe[prefix]; queued {
		s.restoreDedupeMu.Unlock()
		return // already downloading (or queued); a re-trigger is a no-op
	}
	s.restoreDedupe[prefix] = struct{}{}
	s.restoreDedupeMu.Unlock()

	select {
	case s.restoreCh <- restoreJob{ref: ref, prefix: prefix, localDir: localDir}:
	default:
		// Queue full: drop and un-mark so a later CP re-trigger can re-enqueue. The
		// base simply does not appear READY; the CP re-triggers or rebuilds.
		s.restoreDedupeMu.Lock()
		delete(s.restoreDedupe, prefix)
		s.restoreDedupeMu.Unlock()
		s.logger.Warn("noded: restore queue full; dropping enqueue (CP will re-trigger or rebuild)", "artifact", prefix)
	}
}

// restoreWorker drains the restore queue, running each download fire-and-forget.
// A failure is logged, never retried inline (the base simply does not appear
// READY and the CP re-triggers or rebuilds); a success re-registers the base so
// NodeStatus advertises it READY. It exits when ctx is done or the channel closes.
func (s *Server) restoreWorker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case job, ok := <-s.restoreCh:
			if !ok {
				return
			}
			s.runRestoreJob(ctx, job)
		}
	}
}

// runRestoreJob performs one queued BASE download: the multi-GB store.Restore
// (checksum-verified per file), then re-registers the base via
// ReconcileBasesFromDisk so a NodeStatus projection advertises it
// BASE_BUILD_STATE_READY and a later wake sees it. It always clears the dedupe key
// (guaranteed cleanup via defer, mirroring the export worker's crash-safety
// discipline) so a subsequent CP re-trigger can re-enqueue. A download failure is
// logged and dropped: the base stays absent, the CP's poll times out and it
// rebuilds. It uses ctx (daemon lifetime), NOT the original RPC's context, which
// returned at the fast-ACK; that is the whole point of going async.
func (s *Server) runRestoreJob(ctx context.Context, job restoreJob) {
	defer func() {
		s.restoreDedupeMu.Lock()
		delete(s.restoreDedupe, job.prefix)
		s.restoreDedupeMu.Unlock()
	}()

	moved, generation, err := s.store.Restore(ctx, job.prefix, job.localDir)
	if err != nil {
		s.logger.Warn("noded: async base restore failed (CP will re-trigger or rebuild)", "artifact", job.prefix, "err", err)
		return
	}
	s.reregisterRestored(job.ref)
	s.exported.mark(job.prefix, generation)
	s.logger.Info("noded: restored base off store", "artifact", job.prefix, "bytesMoved", moved, "generation", generation)
	s.signalChange()
}

// ---- store reachability probe ----------------------------------------------

// storeProbeInterval is how often the reachability probe refreshes
// store_reachable, mirroring the serving/stateful probe cadence.
const storeProbeInterval = 5 * time.Second

// startStoreProbe launches the periodic object-store reachability probe feeding
// NodeStatus.store_reachable (a warmth hint, never a gate). It no-ops when the
// store is disabled (store_reachable stays false). Runs until ctx is cancelled.
func (s *Server) startStoreProbe(ctx context.Context) {
	if s.store == nil {
		return
	}
	go func() {
		// Probe once immediately so store_reachable is not falsely false for the
		// first interval after startup.
		s.setStoreReachable(s.store.Reachable(ctx))
		ticker := time.NewTicker(storeProbeInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				prev := s.storeReachableNow()
				now := s.store.Reachable(ctx)
				s.setStoreReachable(now)
				if now != prev {
					s.signalChange() // reachability edge is a material change
				}
			}
		}
	}()
}

func (s *Server) setStoreReachable(r bool) {
	s.storeMu.Lock()
	s.storeReachable = r
	s.storeMu.Unlock()
}

func (s *Server) storeReachableNow() bool {
	s.storeMu.RLock()
	defer s.storeMu.RUnlock()
	return s.storeReachable
}

// ---- exported-artifact cache -----------------------------------------------

// exportedCache tracks which artifacts (by store prefix) have a current store
// copy and, for a volume, at which generation. The export queue updates it and
// the NodeStatus projection reads it to set the per-artifact `exported` bool and
// Volume.exported_generation. Safe for concurrent use.
type exportedCache struct {
	mu   sync.RWMutex
	gens map[string]uint64 // prefix -> exported generation (0 for non-volume kinds)
}

func newExportedCache() *exportedCache {
	return &exportedCache{gens: make(map[string]uint64)}
}

// mark records that an artifact's store copy is current at the given generation.
func (c *exportedCache) mark(prefix string, generation uint64) {
	c.mu.Lock()
	c.gens[prefix] = generation
	c.mu.Unlock()
}

// clear forgets an artifact (on remote eviction), so NodeStatus stops reporting
// it exported.
func (c *exportedCache) clear(prefix string) {
	c.mu.Lock()
	delete(c.gens, prefix)
	c.mu.Unlock()
}

// present reports whether an artifact prefix has a current store copy.
func (c *exportedCache) present(prefix string) bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	_, ok := c.gens[prefix]
	return ok
}

// generation returns the exported generation for a prefix and whether it is
// tracked at all.
func (c *exportedCache) generation(prefix string) (uint64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	g, ok := c.gens[prefix]
	return g, ok
}
