# RCA: demo-postgres provisioning wedge (jomcgi.dev/health 503)

Date: 2026-07-19. Status: incident resolved (control-plane restart); this PR
ships the two confirmed-safe hardening fixes and hands the deeper control-plane
fix to review.

## Symptom

`jomcgi.dev/health` returned 503 `{"status":"unhealthy","backendStatus":503}`.
The public `/api/health` aggregate reported its `demo_postgres` component
unhealthy; every real visitor query to the ember Postgres demo failed instantly
with "server closed the connection unexpectedly" (~150ms).

## Confirmed facts and timeline (all 2026-07-19)

- **07:13 / 08:40** Two demo-postgres runtime images were built:
  `runtimes/postgres:...07.13.58-f0b1c6f` then the newer `...08.40.21-191360e`.
- **08:50** The control-plane base builder logged, in a tight loop, for the
  `f0b1c6f` image: `BuildBase ... failed: noded: image "...postgres:...f0b1c6f"
  (workload "demo-postgres") not provisioned on this node`, then
  `BuildBase ... failed: {:connect, :no_addresses}` (a transient registry/DNS
  blip).
- **08:50:29** The noded pod (re)started; its `build-scratch-postgres-rootfs`
  initContainer baked the `191360e` rootfs successfully (`ready=true`). Both the
  `demo-postgres` and `scratch-postgres` Workload CRs reference `191360e`.
- **Through ~15:46** demo-postgres wakes failed. Control-plane
  `/v1/stateful/demo-postgres` showed `state:evicted, pair_valid:false,
  terminal_reason:pair_broken`. noded's cold-boot handed firecracker an EMPTY
  rootfs `path_on_host:""` -> `Drive config error: ... No such file or directory`.
  The banked volume artifact restore also failed (`no meta.json`). So both boot
  sources were unavailable: the base image `f0b1c6f` the control plane was still
  targeting was never provisioned on the node, and the banked pair was broken.
- **scratch-postgres was healthy throughout** (it booted the provisioned
  `191360e` rootfs), which is why the cluster/ArgoCD looked green.
- **The `191360e` image is pullable** (`crane manifest` succeeds), so the 08:50
  `no_addresses` was transient and long gone.
- **15:46 remediation:** `kubectl rollout restart deploy/embervm-embervm`
  (control plane, single-replica, Recreate; GitOps-safe). The fresh CP re-LISTed
  the Workload CRs, resolved demo-postgres to the provisioned `191360e`, and the
  next real wake cold-booted clean (~1.2s connect, fresh postmaster).
  `jomcgi.dev/health` -> 200.

## Why the health check briefly looked green while broken

`ember_public/health.py` is forbidden from connecting to the demo (an asleep
demo is healthy; probing would wake it and defeat the sleep story), so it can
only report the last real-query outcome plus the cached control-plane state.
Condition #4 (last failed wake) holds unhealthy for 10 minutes, then ages out;
with no visitor re-querying, `/health` flipped back to 200 even though the demo
was still unbootable. The `evicted`/`pair_broken` state alone was treated as
healthy (the pair check only fired for `state == "banked"`). Only a real wake
re-exposed the outage.

## What this PR changes (confirmed-safe, testable)

1. **`noded-rootfs-builder-configmap.yaml`: retry crane at bake time.** The
   rootfs-builder initContainer's `crane digest` / `crane export` had no retry;
   a `no_addresses`-class blip at pod-start fails the init and forces a whole
   pod-restart cycle to re-bake. Bounded exponential backoff (5 attempts) around
   both crane reads. This hardens the confirmed trigger (a transient registry
   blip during a bake).

2. **`ember_public/health.py`: surface a stuck fault eviction.** A
   `pair_broken` eviction that persists past the existing 90s stuck window now
   reports unhealthy (a benign `ttl` eviction never does). This makes exactly
   this incident class visible without a manual wake, reusing the same
   stuck-past-threshold idiom the transitional-state check already uses.
   Trade-off: a `pair_broken` eviction is normally recoverable on the next cold
   boot, so this can flag a legitimately-idle-but-cold demo. For demo-postgres
   (idleBankSeconds:1, whose healthy resting state is `banked`), sitting in a
   fault eviction for 90s+ is genuinely abnormal, so the persistence gate makes
   this a net-positive detector rather than a flapping one.

## Root cause of the persistence: open for review (NOT guessed at here)

The transient blip is a confirmed trigger, but it should not have wedged the
demo for ~7 hours: the base builder's failed builds retry with backoff (capped
at 10m, `default_build` has a 600s gRPC deadline so a build cannot hang
forever), and the WorkloadWatcher is a healthy informer. Something kept the
control plane targeting the un-provisioned `f0b1c6f` after the CR moved to
`191360e`. Three candidate mechanisms, none confirmable from the retained logs:

- **H1 - dropped internal reconcile.** `WorkloadWatcher` delivers to
  `BaseBuilder` via `cast_if_alive` (a fire-and-forget cast that no-ops if the
  builder is momentarily unavailable) and has NO periodic internal resync (the
  earlier periodic-LIST loop was removed by design; see
  `workload_watcher.ex:27`). If the `191360e` MODIFIED reconcile was dropped
  (e.g. BaseBuilder mid-restart), nothing re-asserts it until the watcher's next
  apiserver-driven resync (only on watch-end / RV-expiry, potentially hours).
  Proposed fix: a periodic internal resync that re-casts every cached
  `WorkloadCatalog` entry to its consumers (idempotent; BaseBuilder's
  `reconcile_desc` is a no-op for an already-built workload). Standard informer
  pattern applied to the internal consumer edge. **Ranked #1** (covers H1 and H3
  at once, low risk).
- **H2 - BaseBuilder retry stall.** A retry-timer / `already_targeting?` edge
  leaves the desired base unbuilt with no timer and nothing in flight. Analysis
  suggests this state is largely unreachable given the current re-enqueueing
  drop paths (`finish_build` superseded branch re-enqueues), so this is the
  least likely. A periodic BaseBuilder sweep was prototyped and reverted as
  speculative/untestable against current code.
- **H3 - BaseBuilder loses state on restart.** BaseBuilder inits with
  `workloads: %{}`; if it crashed and was restarted by its supervisor
  independently of the watcher, nothing re-seeds it from the watcher's cache, so
  it would never learn demo-postgres at all until the watcher's next resync.
  The H1 internal-resync fix also covers this.

Recommendation: implement H1's internal resync (with an ExUnit test that
populates the catalog, fires no watch events, and asserts BaseBuilder is
reconciled after the interval), and additionally consider surfacing the
Workload's `Ready`/`BaseBuilt` condition in `/v1/stateful/:name` so `health.py`
can flag "base not provisioned" directly rather than inferring from a stuck
eviction. Both are control-plane changes verifiable only via CI + prod
observation, so they are deliberately left for a reviewed follow-up rather than
bundled speculatively here.

## Follow-ups

- [ ] H1: `WorkloadWatcher` periodic internal resync from `WorkloadCatalog`.
- [ ] Surface base-readiness in `/v1/stateful/:name`; consume it in `health.py`.
- [ ] Consider an httpcheck/alert note: this class fires the existing
      `jomcgi.dev/health` downtime alert correctly; no new alert needed.
