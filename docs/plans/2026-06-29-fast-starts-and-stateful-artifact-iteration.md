# Build Plan: Fast MicroVM Cold Starts and Stateful Artifact Iteration

**Status:** Planned (pending ADR 026 acceptance)
**ADR:** [026](../decisions/agents/026-fast-microvm-starts-and-stateful-artifact-iteration.md), builds on [022](../decisions/agents/022-firecracker-snapshot-restore-controller.md) (fc-agentd substrate + `RootfsProvisioner`) and [024](../decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md) (goosecracker artifact tier)
**Created:** 2026-06-29

Executable brief for ADR 026. Two phases: Phase 1 (fast cold starts) is the scalable floor and the fallback path; Phase 2 (stateful iteration) layers on top. Phase 1 is independently shippable and valuable, so ship and validate it before starting Phase 2.

## What already works (do not rebuild)

- **fc-agentd** (node-4, `monolith` ns): reconcile loop claims `PENDING` rows from `claude_agent.agent_threads` every 5s (`ReconcileInterval`), cold-boots a Firecracker microVM via the `driver` package, runs goose, reclaims on Done.
- **`RootfsProvisioner` interface** (`projects/agent_platform/fc-agentd/internal/driver/provisioner.go`) already abstracts per-thread rootfs creation. `CopyProvisioner` (full 3GB copy) is the only impl; the interface comment already names "a devmapper thin-COW impl" as the intended follow-up. The provisioner is selected in `driver.go` (`d.provisioner = &CopyProvisioner{Base: cfg.BaseRootfsPath}`).
- **devmapper on node-4**: proven for devmapper image-seeding (`ctr pull --local --snapshotter devmapper`), so the thin-pool building block exists on the node.
- **goosecracker iteration path**: `chat/goosecracker.py` `continue_session` appends to `chat.goosecracker_sessions.transcript` and calls `dispatch.submit(transcript, recipe="artifact", tier="artifact", discord_thread=thread_id)` (a fresh thread each time = Model B). `ARTIFACT_ID = discord_thread` is injected by `reconcile.envForThread`.
- **Artifact storage**: `s3://artifacts/<id>/index.html` (SeaweedFS), written by the monolith `POST /internal/artifact` (`projects/monolith/artifact/`). Guest holds no S3 cred; it POSTs the file through the egress funnel.
- **Live progress streaming**: guest tees stdout to `POST /internal/goosecracker/progress`; the bot live-edits the thread message. (ADR 024 follow-on.)

## Measured baseline (what we are improving)

- Cold start ~5 to 7s: 0 to 5s reconcile claim + ~2s rootfs copy + ~0.5s boot.
- Iterations re-run the full ~80 to 110s build (Model B); egress capture shows `prompt_tokens_details.cached_tokens = 0` (no inference prefix-cache benefit).

---

## Phase 1: Fast cold starts

### Task 1.1, copy-on-write rootfs provisioner

Goal: per-thread rootfs creation drops from ~2s to milliseconds, behind the existing interface.

- New `RootfsProvisioner` impl in `internal/driver/provisioner.go` (e.g. `DevmapperProvisioner` using a thin-snapshot of the base, or `ReflinkProvisioner` doing a `FICLONE`/`cp --reflink` on a reflink-capable fs for `/disks/nvme-02`). Spike both; pick by what the node-4 filesystem and devmapper thin-pool actually support.
- Select it in `driver.go` via config (`FC_AGENTD_ROOTFS_PROVISIONER`, default = current copy for safety), so it is a flip, not a rewrite. Keep `CopyProvisioner` as the fallback.
- Ensure the thin-snapshot/reflink target is cleaned up on thread teardown (the existing per-thread dir cleanup must release the CoW device/clone, not just unlink a file).
- Validate in-cluster: submit an artifact-tier thread (in-pod runfiles python or DB insert), measure the gap from the reconcile claim to `Listening on API socket` to `assigned task to guest` in fc-agentd logs; confirm it drops from ~2.5s to sub-second and the run still completes and publishes. Confirm per-thread disk usage is the delta, not 3GB (`ls -s` the per-thread rootfs).

### Task 1.2, event-driven dispatch

Goal: remove the 0 to 5s claim wait.

- Wake the reconcile loop on a `PENDING` insert instead of only on the 5s tick. Simplest robust mechanism: Postgres `LISTEN`/`NOTIFY` — `dispatch.submit` (or a DB trigger on `claude_agent.agent_threads`) issues `NOTIFY agent_threads_pending`; fc-agentd `LISTEN`s and runs a reconcile pass on notify. Keep the 5s poll as a safety net (missed notifications, restarts).
- Validate in-cluster: time from `dispatch.submit` / row insert to `Listening on API socket`; confirm it drops from up-to-5s to sub-second, and that a missed notify still gets picked up by the poll (kill the listener briefly, confirm recovery).

### Phase 1 exit criteria

End-to-end cold start (submit to goose running) under ~1.5s, validated on a real goosecracker run; `CopyProvisioner` and the 5s poll both still work as fallbacks.

---

## Phase 2: Stateful artifact iteration (Model A)

Do not start until Phase 1 is shipped (it is the cold fallback for every resume miss).

### Task 2.1, spike: confirm goose session resume semantics

Goal: de-risk the one external dependency before building around it.

- Determine headless goose's resume path: whether `goose run --recipe <r> --resume <session>` works, or whether iterations must use `goose run --resume <session> -t "<instruction>"` (no recipe), and where goose writes/reads its session file (observed under `~/.local/state/goose/...`). Confirm that resuming replays the prior conversation (including prior assistant outputs) so the prefix is stable.
- Output: a short decision note appended to this plan on the exact resume invocation, session file path, and version-compatibility behavior. Everything below depends on it.

### Task 2.2, persist the goose session per thread

Goal: a thread's session survives between runs, stored next to its artifact.

- After a run, the guest exports goose's session file and ships it to the monolith (mirror the artifact publish: `POST /internal/goosecracker/session` with `{id, session_bytes}` through the egress funnel, or extend the artifact publish to carry it). Store at `s3://artifacts/<id>/session.json` (or a `chat.goosecracker_sessions.session_ref`). Keep it small; it is the transcript, not a VM image.
- The artifact file is already persisted in S3; no change there beyond ensuring it is fetchable for restore.
- Validate: run once, confirm `session.json` lands in S3 keyed by the Discord thread.

### Task 2.3, restore and resume on reply

Goal: a thread reply resumes the session instead of cold-rebuilding from the transcript.

- `chat/goosecracker.py` `continue_session`: signal resume mode (a flag on `dispatch.submit`, e.g. `resume=True`) instead of re-sending the full transcript. The new instruction (only the latest reply) becomes the task.
- `fc-agentd` / `fc-agent-init`: when a thread is in resume mode and a session exists, restore `session.json` and the prior `index.html` into the guest's `~/.local/state/goose/...` and `/tmp/artifact.html` before launching goose, then run the resume invocation from Task 2.1 with the new instruction. goose edits the file in place; the existing publish path re-publishes the same `ARTIFACT_ID` (hot reload).
- Validate in-cluster: a two-step thread (build, then "change the color") shows, on the second step, an inference prefix-cache hit (`cached_tokens > 0` in the egress capture) and a markedly shorter build, with the artifact correctly edited (not regenerated from scratch).

### Task 2.4, fallback to cold plus Model B

Goal: correctness when resume is impossible.

- If the session is missing, unreadable, or fails to resume (for example after a goose-version change), fall back to a cold build with the full transcript (current Model B). This must be automatic and logged, never a user-visible failure.
- Validate: delete the stored session for a live thread, send a reply, confirm it cold-rebuilds from the transcript and still produces the right artifact.

### Task 2.5, session TTL and eviction

Goal: stored sessions do not accumulate unbounded.

- A TTL on `s3://artifacts/<id>/session.json` (and optionally the artifact) so abandoned threads are reclaimed. Prefer a SeaweedFS lifecycle/TTL or a periodic reconcile job; do not hand-roll if a bucket lifecycle suffices.
- Validate: confirm an old session is evicted and a subsequent reply cleanly falls back to cold (Task 2.4).

### Phase 2 exit criteria

A multi-reply Discord thread shows incremental edits, shorter per-reply latency, and non-zero `cached_tokens` on iterations, with automatic cold fallback verified.

---

## Sequencing and risk

- **Ship Phase 1 first and independently.** It is valuable alone, low-risk (interface already exists, fallback retained), and is the prerequisite fallback for Phase 2.
- **Phase 2 gates on Task 2.1** (goose resume spike). If headless resume does not preserve the conversation usefully, Phase 2 is reconsidered before building 2.2 to 2.5; Phase 1 still stands.
- **No local test loop**: each task validates in-cluster on a real run (DB insert or `/goosecracker`), reading fc-agentd / egress-proxy logs and the egress `cached_tokens` field. Implement, push, watch CI, validate on the rollout.
- **Chart bumps**: provisioner and dispatch changes are fc-agentd (chart + harness as needed); session persist/restore touches fc-agent-init (harness rebuild) and the monolith (chart). Keep `Chart.yaml` and `deploy/application.yaml` in sync per repo convention.
- **Deploy-cadence caveat (ADR 026)**: Phase 2's value assumes sessions are reused more often than harness deploys invalidate them. If goose-version churn makes resume rarely hit, Phase 1 still carries the latency win and Phase 2 degrades gracefully to cold.
