# Build Plan: goosecracker, Discord-triggered artifact agent

**Status:** Planned (design locked, ready to build)
**ADR:** [024](../decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md), builds on [022](../decisions/agents/022-firecracker-snapshot-restore-controller.md) (fc-agentd substrate) + [023](../decisions/agents/023-egress-secret-proxy.md) (egress secret swap)
**Created:** 2026-06-27

This is the executable brief for the productive Discord agent. The substrate (022 + 023) is live and validated; this plan is the orchestration + artifact layer on top.

## What already works (do not rebuild)

- **fc-agentd** (node-4, `monolith` ns): claims `PENDING` rows from `claude_agent.agent_threads`, cold-boots a Firecracker microVM, runs goose, snapshot-on-idle. `dispatch.submit(task, recipe, discord_thread, ...)` at `projects/monolith/agent/dispatch.py` creates threads; `wake_for_discord_thread()` exists.
- **goose** in the guest has a shell (`--with-builtin developer`), reaches the model + open web through the transparent egress proxy.
- **Egress 6a (transparent funnel)** + **6b (secret swap)** are proven: a guest holding only a `kloak:` placeholder made an authenticated `gh api user` call (sidecar logged `egress swapped`). Chart `projects/agent_platform/fc-agentd/chart`, sidecar code `projects/agent_platform/egress-proxy/cmd/{main,swap}.go`, catalog is `egress.secrets` in values.
- **Discord bot** is message-based (`projects/monolith/chat/bot.py`, `on_message`); `discord_outbox` table + drain exists for posting back. The in-cluster qwen path (`chat/vision.py`) is what the bot already uses for cheap model calls.

## Locked decisions (ADR 024)

| Piece     | Decision                                                                                                                                                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Model     | Gemini 3.5 Flash via OpenRouter; key swapped at the egress proxy (reuses 6b)                                                                                              |
| Tier      | artifact only for v1 (no repo, no `gh` token, zero real secrets in-guest)                                                                                                 |
| Session   | Discord thread == session; **Model B** (re-run goose with the curated transcript: the owner's directed messages + goose's own outputs only, never ambient thread chatter) |
| Trigger   | `/goosecracker <prompt>` slash to start a thread; replies/@s in the thread to continue                                                                                    |
| Gate      | server-side allowlist only (`OWNER_DISCORD_USER_ID`); denied caller gets a qwen roast                                                                                     |
| Artifacts | agent -> monolith publish -> S3 -> sandboxed-iframe `jomcgi.dev/artifact/<id>` + ETag hot-reload                                                                          |

## Prerequisites (done, in 1Password, `vaults/k8s-homelab`)

- `goosecracker/OPENROUTER_API_KEY` (new item).
- `discord-bot/OWNER_DISCORD_USER_ID` (existing item, already synced to a Secret via `vaults/k8s-homelab/items/discord-bot`).

Both stay out of the repo and out of any chat transcript.

## Tasks

### Task 1, model substrate (per-thread tier + OpenRouter)

Goal: an artifact-tier thread reaches Gemini via OpenRouter, key swapped at the proxy.

- New `OnePasswordItem` for `goosecracker` (mirror `projects/monolith/chart/templates/onepassworditem-chat.yaml`; `itemPath: vaults/k8s-homelab/items/goosecracker`) -> Secret with `OPENROUTER_API_KEY`. Put it where fc-agentd's namespace can mount it (it runs in `monolith`).
- `egress.secrets` entry (values): `{ placeholder: "kloak:or:<high-entropy>", env: OPENROUTER_KEY, egressTo: [openrouter.ai], secretRef: { name: goosecracker, key: OPENROUTER_API_KEY } }`. The 6b deployment wiring already mounts the secret + injects the placeholder.
- **Per-thread tiering (the only real new substrate code).** Today `fc-agentd` injects ONE global env map (`config.InjectedEnv` from `FC_AGENTD_INJECT_*`) into every guest. Add a `tier` (or `model`) column to `claude_agent.agent_threads` (migration), a `tier` param on `dispatch.submit`, and have `fc-agentd` choose the injected env by the thread's tier: `artifact` tier -> `OPENAI_HOST=https://openrouter.ai/api`, `GOOSE_MODEL=<gemini-3.5-flash openrouter id>`, `OPENAI_API_KEY=kloak:or:<...>`; default tier -> the existing Qwen env. Tier env templates live in chart values (e.g. `egress.tiers.{artifact,default}`), rendered as `FC_AGENTD_TIER_<NAME>_*` and selected at Assign time.
- Validate (no Discord needed): `dispatch.submit("say hi and name your model", recipe="agent", tier="artifact")` via the in-pod runfiles python (see Validation), then confirm `egress swapped dest=openrouter.ai` in the sidecar and goose names Gemini. Confirm a `default`-tier thread still uses Qwen (no openrouter swap).

### Task 2, monolith artifact service

Goal: publish + serve artifacts, isolated, hot-reloading.

- `POST /internal/artifact` (in-cluster only, not public): body `{id?, html}` -> write `s3://artifacts/<id>/index.html` (SeaweedFS; reuse the monolith's existing S3 client, e.g. the trips/chat-blobs path) -> return `{id, url, version}`. Server assigns `id` when absent. Enforce a max size. **No S3 creds in the guest**, the monolith does the write.
- `GET /artifact/<id>` (public, add to `ALLOWED_PREFIXES`): a thin trusted wrapper page that embeds `<iframe sandbox="allow-scripts" src="/artifact/<id>/raw">` (NO `allow-same-origin`) + the hot-reload poller (polls `/artifact/<id>/version`, reloads the iframe on change).
- `GET /artifact/<id>/raw`: the S3 HTML served with a strict CSP header (`sandbox allow-scripts; default-src ...; connect-src <narrow>`), so even the framed doc is locked down.
- `GET /artifact/<id>/version`: the S3 object ETag/mtime.
- Tighten later: artifact TTL/lifecycle on `s3://artifacts` (SeaweedFS lifecycle / COSI).
- Validate: `curl POST /internal/artifact` with a tiny HTML, open `jomcgi.dev/artifact/<id>`, re-POST same id, confirm the browser reloads and the artifact cannot read `document.cookie`/`localStorage` of `jomcgi.dev` (DevTools console in the iframe throws on `localStorage`).

### Task 3, artifact recipe

Goal: goose builds + publishes.

- A goose recipe `artifact` (`projects/agent_platform/harness/recipes/artifact.yaml`) with `extensions: [developer]` and instructions: build a single self-contained HTML file, then publish it by `curl -XPOST http://<monolith-internal>/internal/artifact` with `{id, html}` and report the returned URL as the result. (The monolith is reachable from the guest via the transparent funnel; no DNS work needed.)
- Validate: `dispatch.submit("make a bouncing-ball canvas demo", recipe="artifact", tier="artifact")` -> goose builds + publishes -> artifact URL appears + renders.

### Task 4, Discord gate + session + result-out

Goal: the owner-only trigger and the Model-B conversation.

- Register a `/goosecracker <prompt>` slash command (new: command registration + interaction handler; the bot is otherwise `on_message`). Visible to all (no Discord permission restriction).
- **Gate (server-side allowlist).** On the interaction (and on every in-thread reply): `if interaction.user.id not in {OWNER_DISCORD_USER_ID}: roast`. The roast calls the existing qwen path with "roast this user for trying a command that isn't theirs" and replies with it. Owner -> proceed.
- **Session store.** Per Discord thread, keep a goose-conversation (owner messages + goose outputs ONLY; exclude the qwen bot's replies and other users). On the first `/goosecracker`, open a Discord thread, store the prompt, `dispatch.submit(transcript, recipe="artifact", tier="artifact", discord_thread=<thread id>)`. On a gated reply in the thread, append + re-submit (Model B re-run with the full curated transcript).
- **Result out.** goose's result + artifact URL -> `discord_outbox` -> posted into the thread.
- Validate: owner `/goosecracker make X` -> thread + artifact link; owner reply "make it blue" -> updated artifact (hot reload); a non-owner `/goosecracker` -> qwen roast, no agent run.

### Task 5, fc-agentd Done -> outbox

Goal: wire the agent result back to Discord.

- On `KindDone`, if the thread has a `discord_thread`, write the result (the goose-result summary + artifact URL) to `discord_outbox` keyed to that thread. The harness should surface its result in the `Done` payload (extend `vsockproto` Done with a result field, or have fc-agentd read the captured goose-result block). The bot drain posts it.

## Cross-cutting notes

- **OpenRouter key = 6b swap**, nothing new: one `egress.secrets` entry on `openrouter.ai`. Per-user routing is automatic, the placeholder is the capability and it's injected only into the owner's (artifact-tier) threads.
- **goose model id format**: OpenRouter ids are `vendor/model` (e.g. `google/gemini-...`); set `GOOSE_MODEL` to the OpenRouter id and `OPENAI_HOST=https://openrouter.ai/api` (goose appends `/v1/chat/completions`).
- **Artifact isolation**: the one footgun is adding `allow-same-origin` to the sandbox, never do it; add a test asserting the wrapper's iframe attributes.

## Validation harness (no SSH)

Submit a thread from the monolith backend pod:

```
kubectl exec -n monolith <monolith-pod> -c backend -- sh -c \
 'cd /projects/monolith/main.runfiles/_main && \
  PYTHONPATH=/projects/monolith/main.runfiles/_main:/projects/monolith/main.runfiles/_main/projects/monolith \
  ./projects/monolith/.main/bin/python3 -c \
  "import agent.dispatch as d; print(d.submit(\"<task>\", recipe=\"artifact\", tier=\"artifact\"))"'
```

Then read fc-agentd + egress-proxy logs (`kubectl logs -n monolith <fc-agentd-pod> -c {fc-agentd,egress-proxy}`). Proof of the model swap is `egress swapped dest=openrouter.ai` in the sidecar. NB: `kubectl exec` into the prod monolith pod is gated by the auto-mode classifier (needs the owner's ok); `python` is not on PATH (use the runfiles path above).

## Gotchas this session already paid for (do not relearn)

- **apko locks cannot be regenerated on macOS** (linux-only binary) and CI `format` does not auto-fix them. So adding a harness package (e.g. python) needs a linux/CI lock step. Gemini can usually emit self-contained HTML+JS with no server-side runtime, so the artifact tier likely needs no python.
- **Doc manifests**: after adding/editing any `.md`, run `python3 projects/monolith/knowledge/tools/gen_repo_docs_manifest.py` and `gen_docs_manifest.py` with `BUILD_WORKSPACE_DIRECTORY=<repo>` set, AND only after `git add`-ing the file (the generators use `git ls-files`). The format hook (prettier) reformats markdown on commit and changes its hash, so regenerate the manifests AFTER the formatting settles (git add the doc, let prettier run once, regen, commit).
- **goose needs `--with-builtin developer`** under `--no-profile` or it starts with 0 extensions (no shell).
- **Raw-FC PID1 must bring `lo` up** (already handled in fc-agent-init); any new guest networking assumption should remember the guest is vsock-only.
- **New GHCR image/chart packages default PRIVATE**; a brand-new image needs a manual visibility flip (anonymous `curl https://ghcr.io/token?...` HTTP 200 = public is the definitive check). The artifact tier adds no new image, but the monolith chart bump path is the usual one.
- **Per-thread tiering is the one genuinely-new substrate change** in Task 1; everything else is wiring on top of proven pieces.
