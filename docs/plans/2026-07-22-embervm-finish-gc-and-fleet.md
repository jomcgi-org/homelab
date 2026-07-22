# EmberVM: finish the program (base GC drained, noded on all four nodes)

**Status:** Proposed
**Created:** 2026-07-22
**Depends on:** ADR embervm/011 (distribution, fencing, CP rollouts), ADR embervm/012
(cpu_sku stamping), the base-durability plan
(`docs/plans/2026-07-21-embervm-base-durability-and-cross-vendor.md`), the R7
distribution spec (`docs/plans/2026-07-18-embervm-r7-distribution-spec-and-plan.md`),
and the fleet rollout and capacity plan (branch doc, commit `4a4e4705a`).

Joe's two headline goals:

1. **The base-retention GC actually enabled and drained** (~290G reclaimed on
   node-4, S3 retention live at N=1).
2. **noded running on every node**: the full 4-node fleet, including the three
   Intel etcd masters.

This plan sequences everything between here and there, with gates, concrete
PRs, and an explicit list of the host actions only Joe can perform.

---

## 0. Critical path

```
Goal 1 (GC):    Phase 1 (#34 settle + digest-ref fix) -> currents durable 9/9
                -> Phase 2 (supervised sweep, ~290G drained)
                -> Phase 3 (PR-4 S3 retention, N=1)

Goal 2 (fleet): fences + durability (DONE) -> Phase 4a (host prep, HOST ACTION)
                -> Phase 4b (first master: label, Intel bases, T2 drill)
                -> Phase 4c (remaining masters hydrate)
                -> Phase 4d (vendor-aware placement + brick distribution)
```

Goal 1 does not block goal 2 and vice versa, with one shared prerequisite:
both need a stable base-build path, which is exactly what Phase 1 delivers.
After Phase 1, the two tracks can run in parallel. Phase 3 (S3 retention) is
deliberately sequenced after Phase 2 but before the masters multiply the
number of S3 base lineages, so the bucket never grows unbounded.

Standing decisions this plan respects and does NOT relitigate:

- Heterogeneous fleet with vendor-bound warmth; bases are per
  (workload, vendor), never cross-vendor (ADR 011, verified against
  Firecracker capabilities in the base-durability plan).
- Fleet = ALL 4 nodes including the etcd masters; blast radius accepted
  (ADR 011/012). Masters onboard via R7, not via the brick cutover.
- S3 retention N=1 per (workload, vendor); rollback to a superseded base is a
  rebuild, accepted (Joe, 2026-07-21).
- R7 cross-node STATEFUL stays blocked on Longhorn HA + backups (Joe,
  2026-07-20). Fleet-noded does not wait for it: stateful workloads stay
  node-4-anchored until R7 Phase 2 lands separately.

---

## 1. Current state (verified live, 2026-07-22)

**Base durability code: fully merged and working.** Base export to S3 (PR-1
#3782), async large-base export (#3786), hydrate-on-miss (PR-2 #3787),
eviction fix plus the gated reconciled retention sweep (PR-3 #3788), and three
SeaweedFS/OCI infra fixes (#3783, #3784 replication 001->000 correction, #3785
s3 gateway 2Gi). All 9 workloads' current bases were durable in S3
(memfile + meta.json) before the churn event below.

**The retention sweep is OFF and proven safe.** Gated by env
`EMBERVM_BASE_RETENTION_SWEEP` (default off = dry-run logging
"WOULD evict N bases"; on = evicts). Currently off (reverted via #3790). A
partial enable evicted only ~26 superseded dirs of durable-current workloads,
protected every current, and deleted nothing un-durable. The mechanism is
inert-and-safe; it needs a healthy build state and supervision to run.

**Task #34, the blocker that parked the reclaim.** During the reclaim attempt,
base builds failed fleet-wide in a tight loop:
`noded: image .../guest:<TS>-dc934d5 (workload X) not provisioned on this
node`. The dc934d5 guest images WERE published to ghcr (verified), so this was
a provisioning-window issue on node-4, not a CI gap. Root cause of the churn
that keeps opening that window: the base-build signature includes the guest
image ref STRING, and the chart renders that ref as `repository:tag` with a
per-build timestamp+commit tag, so EVERY embervm chart publish re-refs all 9
workloads and forces a full base rebuild even when guest content is unchanged.
Five embervm merges in ~2h that night meant five full rebuild storms.

**#34 has settled on its own; the window closed.** Verified 2026-07-22
~00:53 UTC: zero "not provisioned" errors in the fresh noded pod's recent
logs, and new-signature bases are rebuilding and exporting
(`base/amd/<workload>/<ref>` exports observed for cold-image-demo,
hot-image-demo, semgrep, sandbox, demo-postgres so far). So the provisioning
half of #34 is a self-healing window (the roll delivers a fresh SyncRegistry
and the builds catch up); the DURABLE half (stop the churn) is still open and
is Phase 1's PR.

**The churn root cause is precisely located, and the fix is small.** Verified
in code and live:

- The CP base signature is
  `{w.image_ref, zip_sig, vcpus, mem_mib, guest_port, ready_path, init_env}`
  (`control/lib/embervm/base_builder.ex:727-735`), keyed on the ref STRING.
- noded's image-lane base key is
  `sha256(imageRef + revision + vendor)[:12]` (`noded/server/server.go:1851-1875`),
  also ref-string-keyed. (The zip lane already keys on the resolved
  `imageDigest` and is immune.)
- The chart templates render Workload CR refs as
  `{{ .repository }}:{{ .tag }}` (e.g.
  `chart/templates/workload-demo-postgres.yaml:27`, `workload-semgrep.yaml:20`,
  `_noded-pod.tpl:66`, `deployment.yaml:119,139`), and live CRs confirm
  tag-only refs (`...runtimes/postgres:2026.07.22.00.39.37-d58935e`).
- **But `helm_images_values` already emits a `digest` key for every guest
  image** (`bazel/helm/images.bzl:79-90`), added for exactly this reason
  ("charts reference the image as repository@digest so an unchanged image
  renders an identical Deployment"), and guest builds are reproducible:
  `crane digest` on the last four tags of `runtimes/postgres` returns the
  IDENTICAL digest (`sha256:8982508c...`) for all four. The chart simply never
  switched the guest refs from `.tag` to `.digest`.

**Fleet and R7 state.** noded runs only on node-4 today: the DS pod plus the
2gi and 16gi brick canary (PR #3742/#3743). R7 Phase 1 fences are merged and
live: proto contract (`695c26ca6`), vendor detection + vendor-keyed artifacts
(`63775573c`), generation blessing (`a7dcef10b`), DaemonSet over
`homelab.io/firecracker=true` with OnDelete + headless discovery
(`a3882a03f`). The instance-key migration is complete (PR #3762 + alias
deletion #3771, task #22). Longhorn cross-node volumes (R7 Phase 2) are
blocked on the HA + backups prerequisite and are NOT on this plan's critical
path. Masters had kubelet image-GC applied (tasks #15/#16), freeing disk.

**Reclaim scale.** ~289 base version dirs, ~290G to drain, plus one .tmp
orphan (`scratch-k8s__fc281c2c3e10`) on node-4; at least one stale incomplete
S3 object (`scratch-k8s__055eab94dfd0`, no meta.json) to prune remotely.

---

## 2. Phase 1: close #34 (settle, verify, and kill the signature churn)

Gate for everything else. Two halves: immediate (verify the settle) and
durable (the digest-ref PR).

### 2.1 Immediate: verify the settle and restore the durability floor

No code. Fable-supervised op:

1. Confirm zero `not provisioned` errors over a fresh 30-minute window and no
   embervm deploys in flight (STOP merging embervm PRs during this window;
   serialize with any in-flight work).
2. Confirm all 9 workloads' CURRENT bases rebuilt at the new signature and
   exported: `base/amd/<workload>/<ref>` present in S3 with meta.json for all
   9 (CP node facts show `exported: true` for each current; 5 of 9 already
   observed mid-flight).
3. Record the 9 current refs (they are the protected set for Phase 2).

**GATE 1: 9/9 currents durable in S3, build loop quiet.** If a workload stays
un-buildable after settle, that is a new investigation, not this plan.

### 2.2 Durable: PR-F1, digest-pinned guest refs (the churn fix)

One PR, chart-only in mechanism, wide in blast radius awareness:

- Switch every guest-image ref the chart renders from
  `{{ .repository }}:{{ .tag }}` to `{{ .repository }}@{{ .digest }}`:
  - all `workload-*.yaml` `source.image.ref` templates,
  - the `EMBERVM_NODE_IMAGE_IDENTITY` map in `deployment.yaml` (both the
    runtime entry at line ~119 and the identity triples at line ~139),
  - the noded env refs in `_noded-pod.tpl` (line ~66).
  The templates' "must match exactly" comments make the consistency contract
  explicit; every site flips in lockstep in one PR.
- Do NOT change the CP signature or noded's `baseKeyFor`: once the ref string
  is `repo@sha256:...`, an unchanged guest image renders an identical CR, the
  CR generation does not bump, the signature does not move, and no rebuild
  fires. The ref string stays the opaque join key end to end (CP catalog ->
  SyncRegistry -> noded registry -> serving gate), so no proto or Go/Elixir
  change is needed.
- Keep `tag:` in the emitted values (it already feeds the missed-bump guard
  and labels; `images.bzl` retains it deliberately).
- Chart bump via `bazel/tools/git/bump-chart.sh projects/embervm` in the same
  PR.

Expected behavior on the flip: ONE final rebuild storm (ref format changes
for all 9 workloads), then quiet: subsequent CP/noded-only deploys leave refs
untouched and mint zero new bases. This is the acceptance test.

- **Who:** Opus implementer (this is Helm value plumbing several levels deep
  with a cross-language string contract, exactly the CI-only-verifiable class),
  Opus review on the diff before push.
- **Verify live:** merge, let the roll settle, confirm the 9 bases rebuild
  once at digest-keyed refs and export; then merge any trivial embervm PR (or
  the next planned one) and confirm ZERO new base dirs are minted by that
  deploy.

**GATE 2: a chart publish with unchanged guest content mints no new bases,
and 9/9 currents at the digest refs are exported.**

Optional hardening, same PR or fast-follow (small): when a BuildBase is
rejected at the serving gate for an unknown ref, the CP should back off per
workload (it retried at ~1s during #34, 494+ failures). Cheap insurance for
any future provisioning window. Sonnet-able with a clear spec.

---

## 3. Phase 2: drain the ~290G (goal 1, part A)

Fable-run supervised op, gated on GATE 2 (or GATE 1 if Joe wants the reclaim
before PR-F1 merges; the sweep only needs a QUIET build state, but running it
after PR-F1 avoids re-doing the drain after one more rebuild storm).

1. Pre-flight: record the 9 current refs and their `exported` flags; confirm
   the sweep is in dry-run and its "WOULD evict" log matches expectations
   (roughly: all dirs except the 9 currents and any refcounted refs).
2. Enable: set `EMBERVM_BASE_RETENTION_SWEEP=1` in
   `projects/embervm/deploy/values.yaml` (values-only, no chart bump), commit,
   push, let ArgoCD sync.
3. Supervise the invariant: **every current base survives on disk AND in S3**
   throughout. Watch CP sweep logs and node-4 `bases/` dir count converge
   toward 9; disk usage drops ~290G. The drain is one-shot by design (decided
   2026-07-21), so expect a burst of delete IO beside live VMs; watch
   /health and the demo workloads.
4. Also reclaim the `.tmp` orphan `scratch-k8s__fc281c2c3e10` (if the sweep
   does not collect it, remove it via the break-glass evict path and note it).
5. Abort condition: any current base evicted, or wake/prime regressions.
   Abort = flip the flag back to 0 (values revert), investigate.
6. Exit: leave the flag ON permanently. The sweep is the steady-state local
   GC; from here on, superseded bases are collected continuously.

**GATE 3: node-4 `bases/` at ~9 dirs, ~290G freed, flag left on, no
regressions over a 24h soak.** Goal 1 part A complete; update task #31.

- **Who:** Fable (supervised live op; values flips are one-line GitOps
  changes, no implementer needed).

---

## 4. Phase 3: PR-4, S3 retention GC at N=1 (goal 1, part B)

As specified in the base-durability plan section 5 (PR-4), unchanged in
substance:

- **proto:** new `ListArtifacts` verb (prefix in; refs + created-at out,
  bounded), and a `vendor` field on `EvictArtifactRequest` (mirroring
  `RestoreArtifactRequest`) so the CP can address any vendor's prefix through
  any connected node.
- **noded:** implement both; the store client gains a LIST; evict honors an
  explicit vendor over the node-own vendor.
- **CP:** retention sweeper per (workload, vendor): keep the Workload CR's
  current ref plus the newest N-1 by created-at, evict the rest remotely.
  N is one config value, decided N=1 (so: keep current only).
- Confirm the open question from the durability plan before enabling: pinned
  sessions relying on a superseded base are protected by the LOCAL refcount
  guard only; verify sessions pin refs and the local sweep honors them.
- First sweep also collects the stale incomplete S3 objects (e.g.
  `scratch-k8s__055eab94dfd0` with no meta.json): make the sweeper treat a
  ref with no meta.json and age above a threshold as evictable.

- **Who:** Opus implementer (proto + Go + Elixir cross-service change), Opus
  review, end-of-PR CI on the pushed branch. Fable supervises the first live
  sweep.
- **Verify live:** superseded S3 versions collected within one sweep period;
  bucket size stabilizes around the retention envelope (~11.3G per vendor).

**GATE 4: S3 holds exactly the current lineage per (workload, vendor); bucket
stable. Goal 1 complete.**

Phase 3 can start any time after GATE 2 (it does not need the local drain
first), but its live enable should come after GATE 3 so one thing changes at
a time.

---

## 5. Phase 4: noded on all four nodes (goal 2)

Everything mechanical that R7 needs is merged (fences, vendor keying,
blessing, DaemonSet + OnDelete, headless discovery, instance-key dial paths,
hydrate-on-miss). What remains is operational bring-up plus three genuinely
new pieces of work: per-node config entries, the Intel base bootstrap with
the T2 template drill, and per-vendor BaseBuilder targets. Cross-node
STATEFUL (Longhorn volumes, R7 Phase 2) stays out of scope per the standing
decision; stateful workloads remain node-4-anchored throughout this phase.

### 4a. Host prep on the masters (HOST ACTION: Joe)

For each of node-1/2/3 (i5-12500T, Intel), before any labeling:

1. **KVM:** verify `kvm-intel` loaded and `/dev/kvm` present
   (`lsmod | grep kvm; ls -l /dev/kvm`).
2. **Firecracker runtime:** install the firecracker binary, jailer, and the
   guest kernel image at the same host paths node-4 uses (mirror node-4's
   layout; the noded pod expects the host mounts the chart already defines).
3. **Scratch disk:** pick the scratch device/path per master and prepare it
   (fstab bind-mount like node-4's grandfathered setup, or leave it to
   scratch-prep once re-enabled, see 4a-PR below). Decision needed from Joe:
   which disk on each master becomes FC scratch. The masters are thin
   (~247 GiB roots, recently relieved by kubelet image-GC); size the scratch
   allocation conservatively.
4. **Networking:** confirm the tap/bridge prerequisites noded needs are
   available on the master images (same kernel modules as node-4).
5. Do NOT apply the `homelab.io/firecracker=true` label yet; that is step 4b.

Alongside, one small PR lane (implementer work, not Joe):

- **PR-F2, scratch-prep re-enable:** the scratch-prep image's all-zeros
  digest pin was the apko reproducibility casualty; the SOURCE_DATE_EPOCH fix
  has since landed. Republish, verify `helm_images_values` pins a real
  digest, keep `noded.scratchPrep.enabled=false` until verified, then enable
  via values. node-4's fstab bind-mount is grandfathered; scratch-prep must
  no-op there (idempotence is its design). Sonnet implementer, Opus review.
- **PR-F3, per-node config entries:** the DaemonSet landed, but the
  `noded.nodes.<node>` per-node map (nvmeRoot/scratch path, maxLiveVMs,
  requests sized to protect co-resident etcd/platform pods) needs to exist in
  values and be resolved via Downward-API node name (R7 Task 11's remaining
  half). Opus implementer (chart + noded config resolution), Opus review.

**GATE 5: one master passes host prep (kvm, binaries, scratch, network) and
PR-F2/F3 are merged.**

### 4b. First master onboarding (one node only)

1. Joe labels ONE master (suggest node-2, or whichever has the most disk
   headroom after the scratch decision): HOST ACTION,
   `kubectl label node <node> homelab.io/firecracker=true`, plus the matching
   `noded.nodes.<node>` values entry (PR or values commit).
2. The DS schedules noded there; verify register, dial-home, SyncRegistry,
   and that placement does NOT yet send it work it cannot serve (no Intel
   bases exist yet; the base-READY placement gate already protects this).
3. **T2 template drill (parked design decision, decided here):** wire the
   Intel cpu_template into `PutMachineConfig` as an additive field and run
   the drill on real Intel silicon: boot + BuildBase + snapshot restore
   round-trip with a T2-family template (T2CL is the natural candidate for
   Alder Lake; fall back down-family if FC rejects it on 12th-gen). Decision
   rule (from the durability plan, decision 3): attempt the real wire-level
   template; keep it only if the round-trip passes; otherwise continue with
   raw CPUID + label, which is safe within the identical-i5 tier, and record
   the outcome in ADR 011's amendment trail. Fable makes the keep/fallback
   call on the drill evidence; Opus implementer for the wiring.
4. **First Intel base builds:** trigger/allow BaseBuilder to build each
   workload's base on the labeled master. With durability live, each build
   exports to `base/intel/<workload>/<ref>` (~11.3G total for the current
   set). Note SeaweedFS headroom: N=1 retention (Phase 3) keeps the
   two-vendor envelope ~23G against ~41G free, which fits; this is why
   Phase 3 precedes wide fleet growth.
5. Verify an Intel VM boots per workload class enabled there, and node
   pressure stays inside the reserved requests during a 30-minute soak
   (R7 Task 13's drill list).

**GATE 6: one master serves Intel-based VMs; 9 Intel bases exported;
etcd/platform healthy under soak.**

### 4c. Remaining two masters (hydrate, not rebuild)

1. Joe labels node-1 and node-3 (HOST ACTION, after their host prep), one at
   a time, with their values entries.
2. Each new master HYDRATES the Intel bases from S3 (hydrate-on-miss, PR-2
   #3787, vendor-stamped) instead of rebuilding: this is the payoff of the
   durability program and the acceptance test for cross-node hydrate on
   Intel. Verify restore logs, not build logs.
3. Soak each master as in 4b.5 before labeling the next.

**GATE 7: noded Ready on all 4 nodes; masters hydrated, not rebuilt.
Goal 2's headline is met.**

### 4d. Make the fleet real: per-vendor builds, placement, bricks

Without these, the masters are warm but underused:

- **PR-F4, per-vendor BaseBuilder targets:** BaseBuilder today places one
  build fleet-wide; it must build one base per (workload, vendor present in
  the fleet), placed on the largest-budget instance OF THAT VENDOR (the one
  genuinely new BaseBuilder behavior R7 needs, per the durability plan
  section 4.3). This also makes 4b.4's "first Intel builds" self-maintaining:
  after any future guest content change, both vendors rebuild without ops.
  Opus implementer, Opus review.
- **PR-F5, vendor-aware placement (R7 PR-12 subset):** prefer
  warmth/vendor-matched instances (warmth match > warm-tier vendor > any
  eligible). Cross-node stateful moves stay OUT (Longhorn-gated). Opus.
- **PR-F6, bricks on masters (fleet plan Axis B5, Option B):** per-node brick
  ladders via a `bricks.nodes.<node>.desiredReplicas` values map; masters get
  a reduced ladder (suggest 2gi/4gi only, no 16gi beside etcd; Joe confirms).
  Sonnet for the chart templating with an Opus review, since it is
  Deployment-splitting plumbing.
- CP-sequenced rolls across the fleet (R7 Task 12 / Axis B6) and the node-4
  DS prune (Axis B1, needs brick-dispatch proof) are follow-ons after the
  fleet soaks; list them in the tail rather than gating goal 2 on them.

**GATE 8 (program close for this plan): bases per vendor self-maintain, VMs
place onto all 4 nodes vendor-correctly, brick ladders live per node.**

---

## 6. Tail tasks (tracked, not on the critical path)

| Task | Disposition |
| ---- | ----------- |
| #27 op-log Postgres (PR-3b) + deferred BaseBuilder op_log_mod wiring | Do after Phase 3; Sonnet implementer against the merged PR-3a seam, Opus review. |
| #23 ADR 014 consistency-model implementation (PR #3767) | After GATE 7; the fleet makes worker-authoritative state more valuable, not less. Opus. |
| #17 rotate temp sudo pw + node-3 reserved blocks | HOST ACTION (Joe), any time; listed in section 7. |
| #28 monolith ArgoCD app Degraded (no concrete unhealthy resource) | Independent investigation; Sonnet triage first. |
| #25 evict_orphan_snapshots log spam | Likely benign (known); confirm quiet after Phase 2's drain, then close. |
| #32 offsite Pg backups (real DR target) | Independent of EmberVM; schedule separately. |
| Node-4 DS prune (Axis B1 / 3c) | After GATE 8 + one deliberate brick-dispatch proof burst; Joe go-ahead. |
| CP-sequenced fleet rolls (R7 Task 12 / Axis B6) | After the fleet soaks; fold with artifact-decoupling PR-F surge design. |
| R7 Phase 2 Longhorn stateful (cross-node volumes) | Stays blocked on Longhorn HA + backups prerequisite per Joe's 2026-07-20 decision; not part of this plan. |

---

## 7. Host actions (everything that waits on Joe personally)

Consolidated so nothing waits silently:

1. **Master FC host prep (Phase 4a), per node-1/2/3:** verify `/dev/kvm` +
   `kvm-intel`; install firecracker binary, jailer, guest kernel at node-4's
   paths; prepare the chosen scratch disk; confirm tap/bridge networking.
2. **Decide the scratch device per master** (and confirm the reduced brick
   ladder for masters: 2gi/4gi only, no 16gi).
3. **Label the masters** `homelab.io/firecracker=true`, one at a time, on
   Fable's go signal (4b then 4c).
4. **Go-aheads:** Phase 2 sweep enable (supervised drain window), the 4b
   first-master pick, and later the DS prune (tail).
5. **#17: rotate the temporary sudo password and remove node-3's reserved
   blocks** (post-incident cleanup, independent).

---

## 8. Who does what (model routing per CLAUDE.md)

| Work | Owner |
| ---- | ----- |
| Live ops: settle verification, sweep enable + supervision, drills, labeling sequences | Fable (main loop), with Joe for host actions |
| PR-F1 digest refs, PR-F3 per-node config, PR-F4 per-vendor builds, PR-F5 placement, PR-4 S3 retention, T2 wiring | Opus implementers (deep plumbing, CI-only-verifiable), Opus diff review before push |
| PR-F2 scratch-prep re-enable, PR-F6 brick ladder templating, build-reject backoff, #28 triage | Sonnet implementers, Opus review |
| Test execution | None local; end-of-PR CI on the pushed branch, monitored via `gh pr checks` + BuildBuddy MCP |

One comprehensive review per PR at the end of implementation (repo cadence);
implementers self-review before each commit. Batch embervm merges: every
chart publish still rolls noded, and although PR-F1 removes the base-rebuild
cost, a roll is never free (wake windows, demo-postgres auto-wake).

---

## 9. Risks

- **Another embervm merge during Phase 1 settle** reopens the provisioning
  window pre-F1. Mitigation: serialize embervm merges until GATE 2.
- **The digest flip's one-time rebuild storm** doubles as the Phase 1 settle
  test; do not run the Phase 2 drain between the flip and its settle.
- **T2CL on Alder Lake may not pass the drill** (templates target older
  baselines). The fallback (raw CPUID + label) is pre-approved and safe
  within the identical-master tier; the drill decides, not the plan.
- **SeaweedFS envelope:** two-vendor N=1 (~23G) fits in ~41G free, but watch
  the volume-server disk/slot alert during 4b.4's ~11G Intel export burst.
- **Masters are etcd nodes.** Blast radius is accepted, but each labeling
  step soaks with etcd health watched, one node at a time, and the per-node
  requests in PR-F3 are the guardrail. Never bounce all masters together
  (2026-07-21 outage lesson).
- **Strict-CI rebase treadmill:** batch PRs; every merge flips other PRs to
  BEHIND; use `gh pr update-branch --rebase`.
