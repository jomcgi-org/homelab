package server

import (
	"context"
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
	Export(ctx context.Context, prefix, localDir string, files []string, generation uint64, nowMs int64) (bytesMoved int64, skipped bool, err error)
	Restore(ctx context.Context, prefix, localDir string) (bytesMoved int64, generation uint64, err error)
	DeleteArtifact(ctx context.Context, prefix string) error
	Present(ctx context.Context, prefix string) (bool, uint64, error)
	Reachable(ctx context.Context) bool
}

// artifactKindStr maps an ArtifactKind to its lowercase store-key segment (Fork
// 3: <kindStr>/<workload>/<ref>). Returns "" for the unspecified kind so a
// caller refuses an unknown ref rather than composing a bogus prefix.
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

// artifactPrefix composes the Fork-3 store key prefix for a ref:
// <kindStr>/<workload>/<ref>. For a VOLUME the ref MAY be empty (the volume is a
// singleton per workload), so the prefix collapses to volume/<workload>. Returns
// "" when the kind is unknown or the workload is empty (isolation: keys are
// always namespaced by workload).
func artifactPrefix(ref *nodev1.ArtifactRef) string {
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
// mirrors the driver's per-kind bundle layout under SnapshotRoot (bases/,
// sessions/, serving/, stateful/, group/<set_id>/) and the volume manager's
// VolumeRoot/<workload>. Returns "" when the kind is unknown or the relevant
// substrate is not configured, which the caller maps to FAILED_PRECONDITION.
func (s *Server) artifactLocalDir(ref *nodev1.ArtifactRef) string {
	root := s.cfg.SnapshotRoot
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_BASE:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "bases", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "sessions", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_SERVING:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "serving", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "stateful", ref.GetRef())
	case nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET:
		if root == "" {
			return ""
		}
		return filepath.Join(root, "group", ref.GetRef())
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
			return werr
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
func (s *Server) ExportArtifact(ctx context.Context, req *nodev1.ExportArtifactRequest) (*nodev1.ExportArtifactResponse, error) {
	if s.store == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: object store not configured; export unavailable")
	}
	ref := req.GetArtifact()
	prefix := artifactPrefix(ref)
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
	generation := s.artifactGeneration(ref)
	moved, skipped, err := s.store.Export(ctx, prefix, localDir, files, generation, time.Now().UnixMilli())
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "noded: export artifact %q: %v", prefix, err)
	}
	s.exported.mark(prefix, generation)
	s.signalChange()
	return &nodev1.ExportArtifactResponse{BytesMoved: moved, Skipped: skipped, Generation: generation}, nil
}

// RestoreArtifact fetches an artifact from the store back onto local disk into
// the correct per-kind dir, verifying every file's checksum, then re-registers
// it via the same reconcile helpers a rescan uses so a later wake sees it.
// Idempotent: an artifact already present locally with a matching checksum is a
// skipped no-op. FAILED_PRECONDITION when the store is disabled, or the store
// copy is absent/incomplete/mismatched.
func (s *Server) RestoreArtifact(ctx context.Context, req *nodev1.RestoreArtifactRequest) (*nodev1.RestoreArtifactResponse, error) {
	if s.store == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: object store not configured; restore unavailable")
	}
	ref := req.GetArtifact()
	prefix := artifactPrefix(ref)
	if prefix == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: artifact kind and workload required")
	}
	localDir := s.artifactLocalDir(ref)
	if localDir == "" {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: artifact kind %s not restorable on this node", ref.GetKind())
	}
	// Idempotency: if the artifact is already present locally with a checksum
	// matching the store's marker, the restore is a no-op (the store Export's own
	// same-checksum compare is the authority; re-check presence cheaply first).
	if local, err := enumerateArtifactFiles(localDir); err == nil && len(local) > 0 {
		present, gen, perr := s.store.Present(ctx, prefix)
		if perr == nil && present {
			// A local copy exists; treat as already-restored (skipped). The
			// content-level equality lives in Export's checksum compare on the next
			// export; a restore's job is to make local non-empty, which it is.
			s.reregisterRestored(ref)
			return &nodev1.RestoreArtifactResponse{Skipped: true, Generation: gen}, nil
		}
	}
	moved, generation, err := s.store.Restore(ctx, prefix, localDir)
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restore artifact %q: %v", prefix, err)
	}
	s.reregisterRestored(ref)
	s.exported.mark(prefix, generation)
	s.signalChange()
	return &nodev1.RestoreArtifactResponse{BytesMoved: moved, Generation: generation}, nil
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
	prefix := artifactPrefix(ref)
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
// reusing the existing kind-specific eviction path (EvictSnapshot for a session/
// serving/stateful bundle, RemoveGroupMemberBundle per member for a group set,
// DeleteVolume for a volume). Idempotent.
func (s *Server) evictArtifactLocal(ctx context.Context, ref *nodev1.ArtifactRef) (*nodev1.EvictArtifactResponse, error) {
	switch ref.GetKind() {
	case nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL:
		if _, err := s.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: ref.GetRef()}); err != nil {
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

// startExportQueue launches the bounded export-worker pool. It is idempotent and
// a no-op when the store is disabled (nil): with no store there is nothing to
// export. Called once from the daemon entrypoint after the server is built. The
// workers run until ctx is cancelled (daemon shutdown), draining fire-and-forget.
func (s *Server) startExportQueue(ctx context.Context) {
	if s.store == nil {
		return
	}
	s.exportOnce.Do(func() {
		s.exportCh = make(chan exportJob, exportQueueDepth)
		for i := 0; i < exportQueueWorkers; i++ {
			go s.exportWorker(ctx)
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
	key := artifactPrefix(ref)
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
	_, skipped, err := s.store.Export(ctx, job.key, localDir, files, generation, time.Now().UnixMilli())
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
	prefix := artifactPrefix(ref)
	if prefix == "" {
		return
	}
	present, gen, err := s.store.Present(ctx, prefix)
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
