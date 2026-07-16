# EmberVM R3: zip-lane serving base provisioning (PR-7 Part A.5)

Fixes the zip-lane serving cold-start so `serving-og-image` (and every future zip serving workload) can actually serve, plus the edge Host-mismatch that 404s the drill route. Option A1 (build-produced handler artifact), folding A3 (skip the dead-weight snapshot for serving-exclusive bases). C (per-cold-start re-hydrate) is rejected. All Firecracker-touching parts are LIVE-only verifiable (no KVM in RBE), so the plan ends with an ordered live-verification sequence.

## Problem (diagnosed, evidence-backed)

`serving-og-image` is `BaseBuilt=True`, its base snapshot exists on node-4's disk, yet an activator-miss `StartServing(fresh)` returns `noded: serving image "serving-og-image__5213844d3dc1" not provisioned on this node`.

### The chain (file:line, branch feat/embervm-r3-og-image)
1. `startServingFresh` (`noded/server/serving.go:66-68`) looks up `s.cfg.Images[imageRef]` with `imageRef = fresh.snapshot_ref`. `s.cfg.Images` is the STATIC runtime-rootfs table (init-container-baked immutable ext4s keyed by runtime refs like `python312`, `config/config.go:205`), not the base registry.
2. `fresh.snapshot_ref` is the BuildBase base key (`serving-og-image__5213844d3dc1`): `server.go:1215` (`c.SnapshotRef = b.snapshotRef` in NodeStatus) -> `serving_placement.ex:75` (`Map.get(wc, :snapshot_ref)`) -> `serving_manager.ex:417` (`FreshSource{snapshot_ref: base_ref}`). A base key is never an image-table key -> `FailedPrecondition` before any boot.
3. Even if the lookup resolved to the runtime rootfs, the handler is not there: BuildBase's `runBuild` (`server.go:411-457`) produces ONLY a base memory+rootfs SNAPSHOT (`SnapshotBase`, line 453). The zip handler is unpacked to a TMPFS (`shim.py:108-111` `UNPACK_DIR=/tmp/ember-app`; `guest-init/cmd/mount_linux.go:11-14`: "the mutable unpack state lives in the snapshot MEMFILE (RAM)") on a READ-ONLY rootfs, so it lives ONLY in the memory snapshot, never on a block device.
4. Serving must COLD-BOOT with a NIC (D-R3.4.2; FC cannot hot-attach a NIC to a resumed snapshot, `driver.go:478-491` attaches the NIC pre-Start only). A cold boot brings up the bare read-only runtime rootfs with no handler, and the shim imports the handler ONLY on a build-only `POST /shim/hydrate` (`shim.py:229-254`, `_handle_hydrate` 413-436); a serving cold boot never hydrates -> `/shim/ready` 503 -> `finishServingStart` reaps the VM.

### Root cause
A real architectural provisioning-model gap: D-R3.4.2 (serving cold-boots with a NIC) and the zip lane's "handler baked into the base snapshot" (D-R3.11.1) are physically irreconcilable, because the handler lives only in the base memory snapshot (RAM) and a NIC cold-boot cannot resume that snapshot. D-R3.11.1's "a serving cold boot of a zip base already has the handler imported... the ONLY gap was the transport" is FALSE for the zip lane. PR-7 Part A fixed the transport but shipped onto a serving base-provisioning path that does not exist for zip.

The `og-image__5213844d3dc1` vs `serving-og-image__5213844d3dc1` dual snapshot/create is NOT a bug: `baseKeyForZip` (`server.go:1540`) hashes `(runtime_digest, archive_sha256)` and prefixes the workload name; the task-class `og-image` and serving-class `serving-og-image` share the identical runtime+zip, so same suffix `5213844d3dc1`, different prefixes. Two workloads, identical base content, distinct bases. Confirms the diagnosis.

### Two code-validation confirmations
1. Does the shim's cold-boot import read the handler from disk? NO. The shim imports the handler ONLY inside `hydrate()`, driven by a build-only `POST /shim/hydrate`. A plain cold boot leaves `state.handler=None`, `/shim/ready` 503 forever. No on-disk cold-boot import path exists today; A1 must ADD one.
2. Does the build VM's rootfs have the unpacked files on the block device at snapshot time? NO. They are on a tmpfs (`/tmp/ember-app`, `size=256m`) over `/tmp`, because the rootfs is read-only + snapshot-shared; captured only by the memory snapshot's memfile. A rootfs block-device copy captures nothing; there is no writable persistent partition in the guest.

## Decision: A1 (+ A3), C rejected

### ADR amendment text

**DECISIONS.md — append D-R3.11.2:**

> ## D-R3.11.2: zip-lane serving needs a build-produced, cold-boot-readable HANDLER ARTIFACT (corrects D-R3.11.1's "cold boot already has the handler")
> - CORRECTION: D-R3.11.1 asserted "the zip lane bakes the unpacked, handler-imported state into the base snapshot at BuildBase, so a serving cold boot of a zip base already has the handler imported. The ONLY gap was the transport." That is FALSE for the zip lane. The handler is unpacked to a TMPFS (`/tmp/ember-app`, mounted over `/tmp` because the runtime rootfs is READ-ONLY and snapshot-shared) and imported into the running python process; both live ONLY in the base memory snapshot's memfile (RAM), never on any block device. A serving VM must COLD-BOOT with a NIC (D-R3.4.2: FC cannot hot-attach a NIC to a resumed snapshot), and a cold boot cannot resume that memory snapshot, so it comes up on the bare read-only runtime rootfs with NO handler and no on-disk source to import from. The shim's only handler-import path was the build-only `POST /shim/hydrate` from tmpfs. So the serving base-provisioning path did not exist for zip: PR-7 Part A fixed the transport onto a base that a serving cold boot could not consume, which is why `StartServing(fresh)` returned FAILED_PRECONDITION ("serving image ... not provisioned"): the control plane passed the base-snapshot key into `startServingFresh`, which looks it up in the runtime-rootfs image table where it can never appear.
> - DECISION (A1): BuildBase for a serving workload additionally produces a per-workload HANDLER ARTIFACT (`bases/<baseKey>/handler.zip`, the verified archive bytes noded already holds in memory) that a NIC cold-boot can read. `startServingFresh` cold-boots the runtime rootfs WITH the NIC AND attaches the handler artifact as a second READ-ONLY drive; guest-init signals it via an `ember.handler_disk=` boot-arg (mirroring `ember.serving_port=`), and the shim reads the zip off that drive and runs its EXISTING unpack+import (`unpack_archive` + `load_handler`) BEFORE binding TCP, so the serving guest is ready without a network hydrate. This honors BOTH D-R3.4.2 (NIC configured pre-boot) and the baked-at-build invariant (the handler is materialized at build time from the build-time-only archive; nothing is fetched or hydrated per request; the block device is a local, re-derivable artifact, not a portability dependency).
> - MECHANIC (raw-zip drive, not a pre-unpacked ext4): noded writes the sha256-verified archive bytes it ALREADY holds (`driveBuild`'s `archive []byte`) to `bases/<baseKey>/handler.zip` host-side and attaches that file as a read-only drive; the shim reads the raw device and unpacks to tmpfs at cold boot (~ms for a small zip). REJECTED the "build guest copies /tmp/ember-app onto an attached writable ext4" piggyback: it needs a writable drive on the build boot, a new guest-side copy step, and a clean-detach/fs-consistency guarantee, for no benefit, since noded already has the bytes and can write the artifact host-side with zero build-guest change. REJECTED a pre-unpacked ext4 handler-disk (M2): needs `mkfs.ext4`/loop tooling added to the noded apko image and fs-consistency care; the only win is skipping a millisecond boot-time unpack. Raw-zip is strictly less machinery.
> - A3 (skip the base snapshot for serving-exclusive bases): per D-R3.3.1 serving never restores from the base snapshot (it cold-boots), so for a workload registered ONLY as serving the vsock-only memory snapshot is dead weight (built, disk-consumed, never used). BuildBase therefore skips `SnapshotBase` when the base is serving-exclusive, producing ONLY the handler artifact. A workload registered as BOTH task-class and serving-class (og-image) still needs the snapshot for the task lane, so the build branches on the workload's registered class(es): always produce the handler artifact for a serving base; produce the snapshot unless the base is serving-exclusive.
> - REJECTED C (per-cold-start re-hydrate): un-gating `/shim/hydrate` for serving and re-delivering the archive per cold boot re-adds a per-cold-start SeaweedFS fetch and a vsock consumer to serving VMs, and regresses the baked-at-build invariant. Its only value was de-risking the router-match/activator-fire/wake path, which the drill's live 503 ALREADY proves (the miss fired and reached the wake). C is now near-pure throwaway, and the gate evidence must reflect the durable architecture.

**ADR amendment (the zip-lane ADR):** DECISIONS.md attributes the zip lane to "ADR embervm/002", but `docs/decisions/embervm/002-op-log-retention-and-compaction.md` is op-log retention; the zip-lane rationale lives in **ADR embervm/001** (the orchestrator ADR). Add a short amendment note to 001's zip-lane section: "R3 serving cold-boot cannot consume the tmpfs/RAM-baked handler; serving bases additionally produce a cold-boot-readable handler artifact (D-R3.11.2)." Cross-note in 001 only; no new ADR file. (If Joe prefers a dedicated ADR for the serving base-provisioning model, that is a small follow-up; D-R3.11.2 is the authoritative record either way.)

## File-by-file plan (A1 + A3, raw-zip drive)

Order: proto/contract -> noded build -> noded serving inventory + claim -> driver drive -> guest -> control plane -> routing fix -> chart -> tests -> live drill.

### 1. `proto/embervm/node/v1` — the contract carrier (do first; both languages depend on it)
- `BuildBaseRequest`: add a way to mark the base serving (noded writes the handler artifact) and, for A3, serving-exclusive (noded may skip `SnapshotBase`). Prefer a `WorkloadClass`/class-set field mirroring the CR's `class`, so noded derives both behaviors from one signal.
- `WorkloadCapacity` (NodeStatus): add `serving_image_ref` DISTINCT from `snapshot_ref`. The overload (one `snapshot_ref` field feeding both the task-lane snapshot AND the serving cold-boot ref) is the root of the wrong lookup; the serving cold-boot ref must be its own field.
- `FreshSource`: the `snapshot_ref` field now semantically carries the serving-image ref; rename to `serving_image_ref` for clarity (additive-compatible; update both callers). Keep `RelightSource.snapshot_ref` unchanged (relight still uses the per-instance serving snapshot).
- Regenerate Go + Elixir stubs (pure-genrule proto codegen). Blast radius: MEDIUM (ripples to both languages/build graphs; additive fields keep wire-compat).

### 2. `noded/server/server.go` — BuildBase writes the handler artifact + class-aware snapshot skip
- In `runBuild`, after `WaitReady` succeeds (line 450) and for a serving base: write the in-memory `archive` bytes to `d.baseDir(baseKey)/handler.zip` (host-side; noded already has and has sha256-verified them in `driveBuild`). Register the serving-image entry (step 3).
- A3 branch: call `SnapshotBase` (line 453) UNLESS the base is serving-exclusive (from the step-1 class signal). For a dual-class base (og-image) still snapshot. Keep the existing idempotency short-circuits intact for both artifacts.
- Return both refs in `BuildBaseResponse` (snapshot ref when produced; serving-image ref for a serving base). Blast radius: MEDIUM (hot BuildBase path; the class branch is the subtle part — a wrong skip breaks the task lane, covered by tests + the dual-class drill check).

### 3. `noded/server/serving_registry.go` (extend) + `server.go` inventory
- Add a `servingImages` registry: `baseKey -> {handlerArtifactPath, runtimeImageRef, sizeBytes}`, populated by BuildBase (step 2) and RESCANNED from disk on startup by globbing `bases/*/handler.zip` (mirror the serving-snapshot rescan at `driver.go:322-344`), so a daemon/pod restart re-discovers provisioned serving images. Store `runtimeImageRef` in a sidecar (e.g. `bases/<key>/runtime.ref`) so the rescan can resolve the boot rootfs without the control plane.
- `workloadCapacities` (`server.go:1200`) reports `serving_image_ref` from this registry. Blast radius: MEDIUM (new registry + startup rescan + NodeStatus projection; the rescan must not miss or double-count).

### 4. `noded/server/serving.go` — `startServingFresh` resolves the serving ref, requests the handler drive
- Replace the `s.cfg.Images[imageRef]` lookup (line 66) with a serving-images registry lookup keyed by the serving-image ref. From the entry, resolve the runtime rootfs (`runtimeImageRef` -> `s.cfg.Images`) for drive 1 and `handlerArtifactPath` for drive 2. Fix the error message to name the serving image ref and the registry it was missing from.
- Pass `handlerArtifactPath` into `ClaimServing`. Blast radius: LOW-MEDIUM (localized; this is the visible fix).

### 5. `noded/fcvm/driver/driver.go` — second read-only drive on the serving cold boot
- Add `handlerDiskPath string` to `coldBootSpec`. In `coldBoot` after the rootfs `PutDrive` (line 475), if set: `PutDrive{DriveID:"handler", PathOnHost:handlerDiskPath, IsReadOnly:true}` (a `/dev/vdb` second drive). Set an `ember.handler_disk=1` (or the device path) boot-arg in `bootArgsFor` (line 257) ONLY when the handler disk is present, mirroring the serving-only `ember.serving_port=` directive so task/session boot args stay byte-identical. Thread `handlerDiskPath` through `ClaimServing` (line 521). Blast radius: LOW (one conditional PutDrive + one boot-arg; the FC risk is only that the guest sees and can read `/dev/vdb`).

### 6. `runtimes/python/guest-init/cmd/*` + `runtimes/python/shim.py` — the NEW cold-boot on-disk import path (riskiest guest change, live-only)
- guest-init: read the `ember.handler_disk=` token from `/proc/cmdline` (reuse the `servingPortFromCmdline` pattern in `main.go:99-112`) and export `EMBER_HANDLER_ZIP=/dev/vdb` (the raw device). No mount needed (raw-zip mechanic): the shim reads bytes off the device directly. Task/session boots carry no token -> env unset -> path unchanged.
- shim: in `main()` (line 535), when `EMBER_HANDLER_ZIP` is set, `open(path,'rb').read()` the zip bytes and call the EXISTING `hydrate(state, archive)` (reusing `unpack_archive`+`load_handler`) BEFORE `serve()`, so `state.ready` is True at first probe. Keep the vsock/task path byte-unchanged when the env is unset. Update the stale comment at `shim.py:542-544` (it claims a serving cold boot "already has the handler imported"; correct it to describe the handler-disk import). Blast radius: MEDIUM, LIVE-only verifiable. The shim already unpacks+imports; the delta is "read from a device instead of a POST body."

### 7. `control/lib/embervm/serving_placement.ex` + `serving_manager.ex` — pass the serving ref, not the snapshot key
- `serving_placement.ex:75`: for a cold boot return `wc.serving_image_ref` (the new field), not `snapshot_ref`. `base_ready?`/`eligible_for_workload?` gate on the serving-image being present (not the snapshot).
- `serving_manager.ex:417` (`cold_request`): put the serving-image ref into `FreshSource{serving_image_ref: ...}`. `relight` (`serving_manager.ex:408`) UNCHANGED. Blast radius: LOW-MEDIUM (field swap + capacity-fact plumbing; Elixir tests assert on these).

### 8. `control/lib/embervm/base_builder.ex` — mark the BuildBase request serving/serving-exclusive
- Populate the step-1 class field on `BuildBaseRequest` from the Workload's registered class(es), so noded writes the artifact and applies the A3 skip only for serving-exclusive bases. Blast radius: LOW.

### 9. ROUTING FIX (bug 1, edge Host-mismatch 404) — fold into this PR
- DIAGNOSIS: the EndpointPublisher builds the node-Envoy virtual host with `Domains: [serving.host]` (`endpoint_publisher.ex:438` -> `xds/snapshot/desired.go:244`), i.e. `og-image-serving.private.jomcgi.dev` (the workload's `serving.host`, D-R3.11.1). But the drill route is a PATH on the private gateway host, so Cloudflare + the private Envoy Gateway forward `Host: private.jomcgi.dev` (`serving-httproute.yaml:11-13` comment; `drillRoute.matchHost: private.jomcgi.dev` in `chart/values.yaml:203`). The node Envoy has no vhost matching `private.jomcgi.dev` -> 404.
- FIX (chart-only, lowest blast, preserves the per-workload-host vhost model): add a `URLRewrite.hostname` filter to the drill HTTPRoute (`serving-httproute.yaml`) that rewrites the Host to the workload host before the request reaches the node Envoy:
  ```yaml
  filters:
    - type: URLRewrite
      urlRewrite:
        hostname: {{ .Values.servingOgImage.serving.host | quote }}   # og-image-serving.private.jomcgi.dev
  ```
  (URLRewrite precedent: `projects/loom/deploy/httproute.yaml:26`; RequestHeaderModifier precedent: `context-forge-gateway`. `URLRewrite.hostname` is the canonical Gateway-API Host rewrite; `RequestHeaderModifier` on `Host` is rejected by some implementations, so prefer URLRewrite.)
- ALTERNATIVE (rejected for v1): make the publisher additionally emit `matchHost` (private.jomcgi.dev) as a vhost domain for the drill workload. Rejected: it bakes a drill-specific host into the durable publisher path and, with a shared host across future serving workloads, forces path-based routing within one vhost — more machinery than a one-line edge rewrite. The rewrite keeps the publisher's clean per-workload-host model.
- Also remove/repoint the stale `serving-httproute.yaml` comment that references `serving-hello` if still present. Blast radius: LOW (chart template + a values reference; live-verified by the drill).

### 10. Chart bump (REQUIRED, same PR)
- `bazel/tools/git/bump-chart.sh projects/embervm` (noded + guest images + control all change). Blast radius: LOW but mandatory — a code-deploy PR needs the bump in the same PR or the Push-images guard fails.

### 11. Tests (what CI CAN cover without KVM; everything guest/driver/boot is live-only)
- CI-coverable: (Go) serving-images registry populate + startup rescan (glob `bases/*/handler.zip`, resolve runtime ref); `startServingFresh` resolves the serving ref and requests the handler drive (mock driver); driver sets the second PutDrive + boot-arg when the path is set and NOT otherwise (task path byte-unchanged); BuildBase writes `handler.zip` and skips/keeps the snapshot per class (fake build driver). (Python) shim cold-boot-from-device import: feed the existing sample handler via a temp file standing in for the device, assert serving-mode boot imports + serves without a hydrate POST. (Elixir) placement returns `serving_image_ref` for cold; manager builds the cold `FreshSource` with it; publisher route unchanged. New `*_test.go`/`*_test.py` need gazelle `go_test`/`py_test` targets (BDD-completeness guard trips if public callables change).
- NOT CI-coverable (flag live-only): the actual FC cold boot with two drives, the guest mounting/reading `/dev/vdb`, the shim importing off the device, the tap health-gate, and the end-to-end drill.

### 12. Live drill (post-merge, the real acceptance gate)
- Ordered sequence in the next section.

## PR sizing

ONE PR (PR-7 Part A.5), ~12-15 files: proto (both langs) + noded (server build path, serving registry, serving.go, driver) + guest (guest-init, shim) + control (placement, manager, base_builder) + chart (serving-httproute routing fix, bump) + tests. Roughly the size of the original zip-lane build path. All FC-touching parts are live-only verifiable, so the plan is written to one-shot (thorough per-file design + a live sequence), not to lean on CI.

## Ordered live-verification sequence (this is the acceptance gate; CI green != working)

1. Merge + ArgoCD sync; confirm the new embervm chart version is live (`kubectl get applications -n argocd`, noded + control + guest images rolled). UNBLOCKS: everything below.
2. Base build: trigger/observe BuildBase for `serving-og-image`. Verify on node-4: `bases/serving-og-image__<sig>/handler.zip` exists (the new artifact) and the runtime-ref sidecar is written. For a dual-class base, verify the snapshot (`snapfile`+`memfile`) ALSO still exists; for a serving-EXCLUSIVE test workload, verify the snapshot is SKIPPED. UNBLOCKS: NodeStatus reporting the serving image provisioned.
3. NodeStatus: confirm the control plane sees `serving_image_ref` provisioned for the workload (placement `eligible_for_workload?` passes). UNBLOCKS: cold-boot placement.
4. Cold boot: hit the workload (post routing fix, step 6) to fire the activator miss -> `StartServing(fresh)`. Verify noded cold-boots with drive 1 (rootfs) + drive 2 (handler.zip) + NIC, the guest reads `/dev/vdb`, the shim imports and `/shim/ready` returns 200 over the tap, and `finishServingStart` registers the VM (no reap). This is the core fix proven. UNBLOCKS: end-to-end serve.
5. Serve: the handler returns the og-image over the tap (200 + image bytes), the node Envoy proxies it back. UNBLOCKS: the drill.
6. Drill route: `curl https://private.jomcgi.dev/og-image-serving` returns the og-image (200), proving the routing fix (Host rewritten to the workload host, node-Envoy vhost matches). If this 404s, re-check the `URLRewrite.hostname` value == `serving.host`.
7. Regression: confirm the task-class og-image (dual-class) is unaffected (its snapshot intact, task `/invoke` still works), and that task/session guests still boot vsock-only (no handler drive, no serving boot-arg).

## Open items for the controller (none blocking; recorded)
- ADR: cross-note in embervm/001 as above, or spin a dedicated serving-base-provisioning ADR? D-R3.11.2 is authoritative regardless.
- Confirm the raw-zip drive mechanic (over the piggyback-ext4 and pre-unpacked-ext4 variants) — justified above as lowest-machinery; flagging since it diverges from the "copy /tmp/ember-app onto an attached ext4" framing.
