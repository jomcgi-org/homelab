package server

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Rootfs GC (#4088, ROOTFS slice).
//
// The rootfs-builder init container bakes a new rootfs-<ts>-<commit>.ext4 into a
// workload's scratch dir on every image build, and until now NOTHING removed the
// previous ones. Measured on node-4 2026-07-28: 1135 files across 8 workloads,
// ~279 GB, with the registry referencing exactly ONE per workload (semgrep alone
// held 193 files / 119.7 GB).
//
// The workload registry names the rootfs NEW boots will use. Existing base
// snapshots are different: their snapshot state embeds absolute paths to the
// backing rootfs files. GC therefore keeps the union of workload registry refs
// and rootfs paths recorded by READY bases.
//
// Two guards keep that argument honest:
//
//   - Only directories a live registry entry POINTS INTO are swept, derived from
//     the refs themselves rather than from a configured root. A directory the
//     registry knows nothing about is never touched, so an unrecognised layout
//     cannot be damaged by a path assumption.
//   - A file younger than rootfsGCMinAge is spared, which covers the one real
//     race: a bake that has landed on disk but whose SyncRegistry has not yet
//     arrived. Under that age the file looks unreferenced but is about to be the
//     current one.
//
// Node-shared by design: several brick pods share one hostPath scratch, so this
// runs per daemon and is idempotent. Two bricks racing on the same file both see
// os.Remove succeed-or-ENOENT, which is the desired end state either way.
const (
	// rootfsGCInterval is the sweep cadence. Slow on purpose: the accumulation is
	// driven by image builds (hours), so there is nothing to gain from checking
	// often, and each pass stats every rootfs file on the node.
	rootfsGCInterval = 30 * time.Minute
	// rootfsGCMinAge spares a freshly baked rootfs whose registry push has not
	// arrived yet, and prevents reclaiming a temporary from an in-progress bake.
	// Temporaries are never in the keep-set because they are not named by a
	// registry ref or a base's rootfsPath, so they rely entirely on this age guard.
	// The bake is expected to complete well inside this one-hour window. A bake
	// slower than that can have its temporary reclaimed while it is being written.
	rootfsGCMinAge = 1 * time.Hour
)

// isReclaimableRootfs reports whether name is a completed rootfs image or an
// abandoned build temporary that is eligible for the rootfs sweep.
func isReclaimableRootfs(name string) bool {
	return strings.HasPrefix(name, "rootfs-") &&
		(strings.HasSuffix(name, ".ext4") || strings.Contains(name, ".tmp."))
}

// startRootfsGC runs the rootfs sweep on a ticker until ctx is done. It sweeps
// once at startup too: a daemon that has just restarted is the most likely moment
// for a backlog to be sitting there.
func (s *Server) startRootfsGC(ctx context.Context) {
	go func() {
		s.sweepRootfs(time.Now())

		t := time.NewTicker(rootfsGCInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-t.C:
				s.sweepRootfs(now)
			}
		}
	}()
}

// sweepRootfs removes rootfs images no registry entry references. Returns the
// count and bytes freed so tests can assert without reading the log.
func (s *Server) sweepRootfs(now time.Time) (removed int, freed int64) {
	keep := s.registry.rootfsRefs()
	if len(keep) == 0 {
		// An empty registry means "we do not know what is current" (a boot before
		// the first sync), NOT "nothing is referenced". Sweeping here would delete
		// every rootfs on the node. Fail toward keeping bytes.
		return 0, 0
	}
	for _, path := range s.bases.rootfsPaths() {
		keep[path] = true
	}

	// Sweep only the directories the registry itself points into.
	dirs := make(map[string]bool, len(keep))
	for ref := range keep {
		dirs[filepath.Dir(ref)] = true
	}

	// Skip a workload holding any base that does not record its rootfs (every base
	// predating the field). We cannot compute their keep-set, and deleting on absent
	// information is exactly what broke the fleet, so those directories are left
	// whole until their bases turn over. The rootfs layout is one directory per
	// workload, which is the same assumption filepath.Dir above already relies on.
	unknown := s.bases.unknownRootfsWorkloads()

	for dir := range dirs {
		if unknown[filepath.Base(dir)] {
			continue
		}
		ents, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, ent := range ents {
			if ent.IsDir() {
				continue
			}
			name := ent.Name()
			// Failed bakes can leave temporary rootfs files behind. They are
			// reclaimed only in a registry-known workload directory and only after
			// the same min-age guard as completed rootfs images.
			if !isReclaimableRootfs(name) {
				continue
			}
			path := filepath.Join(dir, name)
			if keep[path] {
				continue
			}
			info, ierr := ent.Info()
			if ierr != nil {
				continue
			}
			if now.Sub(info.ModTime()) < rootfsGCMinAge {
				continue
			}
			// Apparent size: these are sparse, so this over-reports what the
			// filesystem returns, and is reported as an estimate.
			size := info.Size()
			if rerr := os.Remove(path); rerr != nil {
				if !os.IsNotExist(rerr) {
					s.logger.Warn("noded: rootfs gc could not remove", "path", path, "err", rerr)
				}
				continue
			}
			removed++
			freed += size
		}
	}

	// Always log the shape of the pass, including a no-op one: a sweep that is
	// silent when it finds nothing is indistinguishable from a sweep that is not
	// running at all.
	s.logger.Info("noded: rootfs gc swept",
		"dirs", len(dirs), "kept", len(keep), "removed", removed, "freed_bytes_apparent", freed)

	return removed, freed
}
