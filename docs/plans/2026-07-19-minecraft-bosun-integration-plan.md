# Minecraft AI Bot as `projects/minecraft` + Bosun Control (Phase 1)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan
> task-by-task. This is a picked-up-later plan; validate current code paths before editing
> (file/line refs below may have drifted).

**Goal:** Move the working local Minecraft AI bot (mindcraft + on-cluster Qwen + ViaProxy)
into the homelab monorepo as `projects/minecraft`, run it 24/7 in-cluster (day-gate managed,
autonomous mission), expose a **status + command HTTP API**, and give **bosun** (the monolith
Discord bot) an **owner-only** tool to report status and send commands. Task **queue +
scheduling** is Phase 2 (designed here, built next).

**Architecture:** One pod, one container (Node + JRE), running a Node **control-plane** that is
the entrypoint and supervises two child processes — **ViaProxy** (Java jar, version-translates
1.21.11↔the server's 26.2 and holds the Microsoft auth) and **mindcraft** (`node main.js`, the
bot + its socket.io "mindserver"). The control-plane owns the day/night gate and exposes the
API on `:8080` (the Service port). Bosun calls it over in-cluster HTTP. LLM calls go to
`inference.inference.svc.cluster.local:8080` directly (no port-forward). State (bot memory +
ViaProxy token cache) lives on a RWO PVC.

```
 bosun (monolith) ──httpx──► control-plane (Node :8080) ──spawns──► ViaProxy (Java, :25568)
                              • GET /status  POST /command  /healthz          │
                              • day-gate: log off at dusk, back at dawn        ▼
                              • proxies commands to mindserver        mindcraft (node main.js)
                                                                       • mindserver socket.io :8091
                                                                       • bot → ViaProxy → onlyscotts.com
   PVC /var/lib/minecraft (memory, ViaProxy saves)   LLM → inference.inference.svc:8080
```

**Tech Stack:** Node.js 22 + a JRE (apko/Wolfi image), Bazel `apko_image`, ArgoCD + Helm,
1Password Operator (`OnePasswordItem`), Cilium network policy, Kyverno (non-root uid 65532,
read-only rootfs). Monolith side: Python (FastAPI + PydanticAI + FastMCP + SQLModel/Postgres).

**Source of the working local implementation:** `~/repos/bosuncraft` — mindcraft submodule +
`patches/` (tick_end anticheat fix, unstuck soft-recovery), `config/` (settings.js, bosun.json
with the survival directive, mission.txt), and scripts `daygate.sh` / `bridge.mjs` / `dev.sh`
that this plan consolidates into the control-plane. Key learnings baked into that code:

- Server is Paper **26.2** (proto 776); mineflayer maxes at 1.21.11 → **ViaProxy** bridges it
  and does the online-mode Microsoft auth (account stored in ViaProxy `saves.json`).
- **tick_end patch** (`mindcraft/src/utils/mcdata.js`): emit `CLIENT_TICK_END` each physics
  tick or the server's LoginGuard anticheat kicks the moving bot (mineflayer#3800).
- **unstuck patch** (`mindcraft/src/agent/modes.js`): reset movement instead of `cleanKill`
  when stuck, so a transient stuck doesn't force a full reconnect.
- **Survival directive** (bosun.json `conversing`) + **day-gate**: bot bunkers/logs off at
  night (mobs), works in daylight — cuts deaths and death-feed noise dramatically.

---

## Reference patterns (verified in-repo)

- **Deploy**: `projects/inference/deploy/**` (ArgoCD Application + Helm chart), multi-container
  reference `projects/monolith/chart/templates/deployment.yaml`. apko macro
  `bazel/tools/oci/apko_image.bzl` (`apko_image(config, contents, repository, tars, ...)`,
  dual-arch). Node apko examples: `projects/monolith/frontend/apko.yaml`,
  `projects/firecracker/goosecracker/guest/apko.yaml`.
- **Kyverno**: non-root uid 65532 + read-only rootfs (`docs/decisions/tooling/002-service-deployment-tooling.md`)
  → writable volumes for `/var/lib/minecraft` (PVC) and `/tmp` (emptyDir).
- **Secrets**: `OnePasswordItem` CRD (see `imagePullSecret.onepassword.itemPath` in inference
  values). vLLM in-cluster: `inference.inference.svc.cluster.local:8080`.
- **Bosun tools**: `@agent.tool` + `@signposted(...)` in `projects/monolith/chat/agent.py`
  (existing tools ~L562+); in-cluster HTTP via `httpx.AsyncClient` (pattern
  `projects/monolith/hikes/forecast.py`).
- **ACL**: `projects/monolith/chat/acl.py` — `is_granted(guild_id, subject_id, feature, scope)`,
  allow-list only, grants in `chat.discord_feature_grant`, owner via `OWNER_DISCORD_USER_ID`
  and default seeds (~L134-153). Seeding only the owner for a new `"minecraft"` feature makes
  the tool owner-only by construction.
- **Task queue (Phase 2)**: `claude_agent.routine_jobs` (Postgres, TTL-lock claim via
  `SELECT FOR UPDATE SKIP LOCKED`), MCP tools `monolith_agent_register_routine_job` / `claim`
  / `complete` / `list` / `trigger` (`projects/monolith/agent/routine_jobs.py`, `agent/mcp.py`).
  **NATS is gone — do not use it.**

---

### Task 1: Vendor the bot into `projects/minecraft/`

- Add mindcraft as a submodule at `projects/minecraft/mindcraft` (homelab already uses
  `.gitmodules`). Copy over `patches/` and `config/` from `~/repos/bosuncraft`.
- Update `config/settings.js`: model `url` → `http://inference.inference.svc.cluster.local:8080`
  (served model `qwen3.6-27b`); `mindserver_port` → 8091 (frees 8080 for the API); keep
  `auth: offline`, `host/port` → the local ViaProxy bind. Keep the survival directive in
  `config/bosun.json` and `config/mission.txt`.

### Task 2: Control-plane Node service (`projects/minecraft/control-plane/`)

Consolidate `daygate.sh` + `bridge.mjs` into one supervisor that is the pod entrypoint:

- Spawn + supervise ViaProxy (`java -jar … cli --bind 127.0.0.1:25568 --target
  minecraft.onlyscotts.com:25565 --target-version 26.2 --auth-method ACCOUNT
  --ignore-protocol-translation-errors true`) and mindcraft (`node main.js`); restart on crash.
- Day-gate loop: read mindserver `get-full-state` `gameplay.timeOfDay`; at dusk (tick ≥12200)
  stop mindcraft (logging off **keeps inventory**), sleep the computed night, reconnect at
  dawn, re-assert `mission.txt` via the mindserver `send-message` each daylight connect.
- HTTP API on `:8080`:
  - `GET /status` → `{online, phase:"active"|"sleeping(night)", timeOfDay, health, position,
    biome, activity, mission}`.
  - `POST /command {message}` → inject via mindserver `send-message` as sender `"bosun"`
    (already whitelisted in `only_chat_with`).
  - `GET /healthz`, `GET /ready`.
- Apply `patches/` to the submodule at build time (or first-boot entrypoint step).

### Task 3: Image — `projects/minecraft/apko.yaml` + `BUILD`

- apko: `nodejs-22`, `openjdk-21-jre`, `ca-certificates-bundle`, `busybox`; `run-as: 65532`;
  writable `/var/lib/minecraft`; `cmd` = control-plane entrypoint.
- Bazel: `apko_image(name="image", config="apko.yaml",
  contents="@minecraft_lock//:contents",
  repository="ghcr.io/jomcgi/homelab/projects/minecraft", tars=[":app_tar"])`. `app_tar` layers
  mindcraft + `node_modules` + control-plane + config + the ViaProxy jar under `/app`.
- Register in image push: `bazel run //bazel/images:generate-push-all`.

### Task 4: Deploy — `projects/minecraft/deploy/` (mirror `projects/inference/deploy/`)

- `application.yaml` (ArgoCD → ns `minecraft`), `Chart.yaml`, `values.yaml`, `templates/`:
  `deployment.yaml` (strategy `Recreate`, replicas 1, non-root/RO-rootfs securityContext,
  probes → `/healthz` `/ready`, env `VLLM_URL`/`BOSUN_MC_NAME`, volumes: PVC state + tmpfs
  `/tmp`), `service.yaml` (ClusterIP :8080), `pvc-state.yaml` (RWO), `onepassworditem.yaml`
  (ghcr pull secret + auth blob), `_helpers.tpl`, `cilium-policy.yaml` (egress: `inference` +
  DNS + the external Minecraft server only; ingress: **monolith** only). **Not** exposed via
  Cloudflare — internal only.

### Task 5: Auth secret (ViaProxy Microsoft account)

- The MS account lives in ViaProxy `saves.json` (generated once locally via the GUI
  device-code login — mineflayer connects offline to ViaProxy, so only ViaProxy needs it).
- Store the blob in 1Password (Document or base64 field), sync via `OnePasswordItem`, mount
  read-only. Token refresh needs write → entrypoint **copies** it to the writable PVC on first
  boot; ViaProxy uses the PVC copy. Document the one-time `viaproxy-login.sh` →
  upload-to-1Password step in the project README.

### Task 6: Bosun tool — **owner-only** (`projects/monolith/chat/`)

- Add `@agent.tool @signposted(...) async def minecraft_control(ctx, action, details="")` in
  `chat/agent.py`. First line ACL gate:
  `if not acl.is_granted(ctx.deps.guild_id, ctx.deps.author_id, "minecraft"): return "not
  permitted"`. Confirm `ChatDeps` carries `guild_id` (it has `channel_id`/`author_id` today —
  add `guild_id` if missing). Actions: `status` (GET /status → human summary), `command`
  (POST /command). Reach `http://minecraft.minecraft.svc.cluster.local:8080` via
  `httpx.AsyncClient(timeout=…)`.
- Seed the owner grant: add `"minecraft"` to the owner defaults in `chat/acl.py`
  (`_default_grants`, ~L134-153); allow-list-only ⇒ everyone else denied. Add an `acl_test.py`
  case (owner granted, non-owner denied).
- Optional: a standing directive (via `monolith-chat-set-directive`) telling bosun how to use
  the tool (report status, confirm risky commands).

**Defense-in-depth:** (1) network — API is ClusterIP, Cilium ingress allows only monolith;
(2) ACL — bosun tool owner-only. Non-owner Discord users are denied even inside the monolith.

---

## Security note (LLM code execution)

The bot runs `allow_insecure_coding: true` — the LLM `eval`s generated JS (mineflayer skills)
in-process. The homelab runs untrusted code in Firecracker microVMs; this pod is **not** that.
Mitigations: restricted securityContext (non-root, RO rootfs, drop ALL caps) + a **tight
Cilium egress** (only `inference`, DNS, and the Minecraft server — no general internet, no
lateral movement). Acceptable for a personal bot with that egress fence; capture it in an ADR.
Later hardening could run mindcraft's coder inside fc-invoke/EmberVM.

## Phase 2 (designed, not built now)

- **Task queue / scheduling**: bosun queues work via `monolith_agent_register_routine_job(
  kind="minecraft", payload={…}, next_run_at=…)`; the control-plane polls `list`/`claim`
  (kind=minecraft, TTL lock), executes, `complete`s with `status`+`summary`. Gives "queue up
  tasks", scheduled/recurring work, and progress bosun can query — reusing existing Postgres +
  MCP surface, no new infra.
- Richer per-task progress (mission checkpoints) surfaced through `/status`.

## Verification (end-to-end)

1. **Image**: `bazel build //projects/minecraft:image`; `image.load` + run locally (Node+JRE
   boots ViaProxy+mindcraft, `/healthz` 200).
2. **Deploy**: merge → BuildBuddy pushes image → ArgoCD syncs `minecraft` ns; pod Running,
   `/ready` green; logs show ViaProxy auth (from mounted blob) + bot spawn + day/night gate.
3. **API**: from a monolith pod, `curl http://minecraft.minecraft.svc:8080/status` returns live
   state; `POST /command {"message":"say hi"}` makes the bot chat in-game.
4. **Owner path**: as owner in Discord, "what's the minecraft bot doing?" → status summary;
   "have it come to spawn" → executes in-world.
5. **ACL**: non-owner request refused; `acl_test.py` passes; Cilium blocks a non-monolith pod
   from `/status`.
6. **Autonomy**: over a Minecraft day/night, deaths stay low (logs off at night), resumes the
   `mission.txt` objective each dawn.
