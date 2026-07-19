# Store export drill: the SeaweedFS embervm collection

**Status:** Run 2026-07-19 (R7 plan Task 2, PR-1). Export seam verified live;
the destructive restore half is deferred to the R6/R7 closure gates.

## Symptom

Every noded artifact export failed with:

```
No writable volumes for collection:embervm
```

The R6 durability seam (bank commit -> async ExportArtifact -> SeaweedFS S3)
appeared never to have worked. The bucket listing corrected that: exports DID
succeed on 2026-07-18 between 10:42Z and 12:32Z (about 330 objects, complete
stateful bundles with meta.json markers), then stopped cold at 12:32Z.

## Diagnosis (recorded so the next slot exhaustion takes minutes, not hours)

1. Master topology, read-only:

   ```
   kubectl exec -n seaweedfs seaweedfs-master-0 -- \
     wget -qO- http://localhost:9333/dir/status
   ```

   Evidence: `"Free": 0`, `Max: 106`, and the embervm layout row present with
   `"writables": []` while every older collection held 4-10 writable volumes.

2. The limits behind those numbers, from the running processes:

   - master: `-volumeSizeLimitMB=1000`
   - volume server: `-max 0` (auto)

   Auto max derives from disk capacity / volumeSizeLimitMB, about 106 slots on
   the 100Gi PVC. The pool saturated with the disk only 34% full (33.2G used):
   slots are counted by nominal capacity, not by actual fill. Collections
   allocate writable volumes on demand; embervm, the newest, arrived after the
   pool was exhausted at 12:32Z and could never allocate any.

## Fix

`projects/platform/seaweedfs/values.yaml`: `maxVolumes: 0` -> `130` (explicit,
about 24 spare slots), merged as PR #3686. The bucket itself already existed;
no `weed shell` provisioning was needed. Do NOT enable the chart's
`s3.createBuckets` for this (it flips auth for every consumer).

## Evidence (2026-07-19)

- 01:45Z: volume pod restarts with the new limit; `/dir/status` shows
  `Free: 24`.
- 01:48-01:49Z: within 4 minutes, the blocked export flushes with NO manual
  action: a complete fresh stateful bundle lands at
  `stateful/scratch-postgres/state-6a0fe6fa3d275263/`
  (gen, snapfile, memfile, pinned-ip, meta.json written last, per the R6
  write-ordering contract), and `Free` settles at 17 as embervm claims its
  writable volumes.
- 01:52Z: noded rolls to chart 0.1.121; its startup reconcile finds the
  backlog already durable (enqueueIfMissing marks present artifacts instead of
  re-exporting) and refreshes the volume gen sidecars
  (`volume/demo-postgres/gen`, `volume/scratch-postgres/gen` at 01:55Z).

Bucket listing used for all of the above:

```
kubectl exec -n seaweedfs <s3-pod> -- \
  wget -qO- "http://localhost:8333/embervm?list-type=2"
```

## Deferred: live restore

The read half of the seam (RestoreArtifact on a true local miss) is exercised
by the CI round-trip tests but has NOT been drilled live, because the honest
drill deletes the current local bundle of a live workload. That is R6 gate 6
(and R7 gates 6/8), to be run in a stable no-deploy window, not casually.

## Watch items

- Free slots: alert-worthy below ~8. Today: 17 free of 130. The next
  exhaustion presents as this exact symptom; bump `maxVolumes` again or raise
  the master's `volumeSizeLimitMB`.
- This store is also the planned Longhorn backupstore target (R7 plan Task 7),
  which will consume additional slots.
