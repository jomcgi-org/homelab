# goosecracker resume handoff (Tasks 3-5)

**Status:** In progress, one active blocker. Tasks 1 + 2 SHIPPED + validated in-cluster.
**Date:** 2026-06-28
**Source of truth for the design:** `docs/plans/2026-06-27-goosecracker-discord-artifact-agent.md` + `docs/decisions/agents/024-discord-agent-hosted-model-tiers-and-artifacts.md`. Do NOT re-derive the design.

This is a cold-start handoff so a fresh (cloud) session can continue. It records exactly what works, the one thing that does not, the leading fix, and the remaining tasks.

## TL;DR

goosecracker is a Discord-triggered "make me a thing" agent: `/goosecracker <prompt>` (owner only) runs goose (Gemini 3.5 Flash via OpenRouter, key swapped at the egress hop) in a Firecracker microVM, which builds a self-contained HTML artifact, publishes it to `s3://artifacts/<id>`, and serves it sandboxed at `jomcgi.dev/artifact/<id>` with hot reload.

- **Task 1 (model tier substrate): DONE + validated.** Per-thread `tier` selects the injected model env: `artifact` -> Gemini via OpenRouter (key swapped), default -> in-cluster Qwen. (PR #2890)
- **Task 2 (artifact service): DONE + validated.** `POST /internal/artifact` -> S3; `jomcgi.dev/artifact/<id>` sandboxed-iframe wrapper + `/raw` (strict CSP) + `/version` (ETag) + hot reload. Validated by direct curl. (PR #2891)
- **Task 3 (artifact recipe + publish): BUILT, BLOCKED on model behavior.** All plumbing works; goose just does not write the file for a content-generation prompt (see Blocker).
- **Task 4 (Discord gate + Model-B session): NOT STARTED.** Surface mapped (see below).
- **Task 5 (fc-agentd Done -> discord_outbox): PARTLY DONE.** The Done message already carries the artifact URL (`Result` field); the outbox write + per-thread `ARTIFACT_ID` remain.

## THE BLOCKER (resolve this first)

goose + **Gemini 3.5 Flash via OpenRouter** reliably makes _mechanical_ tool calls (`echo HELLO > /tmp/test.txt` worked) but for a _content-generation_ task ("build a bouncing-ball HTML and write it to /tmp/artifact.html") it loads, prints `goose is ready`, makes ~2 OpenRouter chat/completions calls over ~20s, then exits having emitted **no tool call and written no file**. `fc-agent-init` logs `artifact: nothing to publish ... no such file or directory`.

- Reproduced under BOTH goose providers: the generic `openai`-compatible provider (PR #2890 original) AND goose's native `openrouter` provider (PR #2897). Same empty-turn symptom either way, so it is NOT a provider-plumbing issue.
- OpenRouter calls succeed (egress-proxy logs `egress swapped dest=openrouter.ai env=OPENROUTER_KEY path=/api/v1/chat/completions`), so auth + transport are fine.
- Looks like reasoning-only / `content:null` (Gemini Flash thinking consumes the turn without an actionable content/tool_call). This is the same CLASS as the repo's known "Qwen thinking -> content:null" gotcha (the Qwen path disables thinking via `chat_template_kwargs.enable_thinking=False`).
- **Owner's steer (do not ignore): "Gemini Flash is meant to be used WITH thinking" - do NOT disable thinking. Keep Gemini; make thinking produce an actionable turn.**

### Leading fix (high confidence, try first)

goose exposes the thinking knob via `GOOSE_PREDEFINED_MODELS` (from goose docs):

```
GOOSE_PREDEFINED_MODELS=[{"name":"google/gemini-3.5-flash","provider":"openrouter","request_params":{"reasoning":{"effort":"high"}}}]
```

- For the native `google` provider goose docs use `request_params:{"thinking_level":"high"}`; for OpenRouter the equivalent is OpenRouter's `reasoning` param (`{"effort":"high"}` or `{"max_tokens":N}`). The exact key that goose forwards through the openrouter provider is UNVERIFIED - confirm with the diagnostic below before committing.
- Inject it as a per-tier env on the artifact tier (chart-only, fast roll): add to `egress.tiers.artifact` in `projects/agent_platform/fc-agentd/chart/values.yaml` (it renders to `FC_AGENTD_TIER_artifact__GOOSE_PREDEFINED_MODELS` and lands in the guest env).
- Also worth testing: goose's dedicated `gemini-cli` provider, or the native `google` provider with `thinking_level` (needs a Google API key, not OpenRouter - breaks the egress-swap design, so prefer OpenRouter `reasoning`).

### How to SEE what is actually happening (diagnostic already merged)

A guest diagnostic is merged (PR from branch `chore/goosecracker-guest-diag`): on harness exit `fc-agent-init.dumpGuestDiag()` logs, host-side (panic-immune, captured by fc-agentd), a listing of `/tmp` + `$HOME` + `/` + `/workspace` and the **tail of goose's newest session/log file** (`*.jsonl`/`*.log` under `$HOME`). The goose session log shows the raw model turns: whether Gemini emitted a `tool_calls`, returned `content:null`, or hit `finish_reason: length`. Read it after a run:

```
kubectl logs -n monolith <fc-agentd-pod> -c fc-agentd --since=6m | grep -iE '"msg":"diag:'
```

Use this to confirm the exact failure shape, then apply the precise `reasoning` param. **REVERT this diagnostic (the `dumpGuestDiag` call + function + the io/fs, path/filepath imports) once the model behavior is fixed** - it is debug-only and dumps guest fs/log to controller logs.

## How to iterate (no local test loop; in-cluster only)

The inner loop is slow; plan around it.

- **Submit a thread** from the monolith backend pod (dispatch is just a DB insert; any monolith pod works):
  ```
  POD=$(kubectl get pods -n monolith -l app.kubernetes.io/name=monolith -o name | grep -v searxng | head -1)
  kubectl exec -n monolith ${POD#pod/} -c backend -- sh -c 'cd /projects/monolith/main.runfiles/_main && PYTHONPATH=/projects/monolith/main.runfiles/_main:/projects/monolith/main.runfiles/_main/projects/monolith ./projects/monolith/.main/bin/python3 -c "import agent.dispatch as d; print(d.submit(\"<task>\", recipe=\"artifact\", tier=\"artifact\"))"'
  ```
  For multi-line submit scripts use `kubectl exec -i ... -- sh -c 'cat > /tmp/x.py'` with a heredoc (the `-i` is REQUIRED for stdin), then run the runfiles python on it.
- **Read the run**: fc-agentd pod, container `fc-agentd` shows goose's console + `diag:`/`artifact:` lines; container `egress-proxy` (panic-immune) shows `egress swapped/allowed` (the reliable oracle for what the guest reached).
- **The guest kernel PANICS on PID-1 (fc-agent-init) exit at the end of every one-shot run** ("Attempted to kill init") - this is NORMAL teardown, but it truncates the guest serial console, so trust the egress-proxy logs + the `diag:`/`artifact:` lines (logged before exit) over the goose console.
- **S3 check** (did a publish land): boto3 list in-pod -
  ```
  # write /tmp/lsart.py (boto3 list_objects_v2 on ARTIFACTS_S3_BUCKET, endpoint SEAWEEDFS_S3_ENDPOINT, creds duckdb/duckdb) then run via the runfiles python
  ```
- **Rollout cadence**: a CHART-only change (e.g. tier env) -> fc-agentd rolls fast, NO base-rootfs rebuild. A HARNESS change (recipe, fc-agent-init, the harness image) -> CI rebuilds the harness image -> the fc-agentd base-rootfs initContainer rebuilds the ext4 (~4-5 min) on the next roll. After merge: `monolith-k8s-sync-argocd-app canada` (propagates the new chart `targetRevision` from the app-of-apps), then wait for the fc-agentd pod `3/3 Running`. fc-agentd is in ns `monolith`; chart is OCI-versioned (`projects/agent_platform/fc-agentd/chart`, bumped by chart-version-bot on push).

## What is already built for Task 3 (do not rebuild)

- `projects/agent_platform/harness/recipes/artifact.yaml`: write-only recipe (goose writes `/tmp/artifact.html`; the harness publishes). Imperative "ACT, DO NOT CHAT" instructions. (Auto-globbed into the harness image; no BUILD change for new recipes.)
- `egress.tiers.artifact` in the fc-agentd chart: `GOOSE_PROVIDER=openrouter`, `OPENROUTER_API_KEY=kloak:or:...` (placeholder swapped at egress), `GOOSE_MODEL=google/gemini-3.5-flash`, `ARTIFACT_PUBLISH_URL=http://monolith.monolith.svc.cluster.local:8000/internal/artifact`.
- `egress.funnelPorts: "80,443,8080,8000"` -> injected as `EGRESS_PORTS` so the guest funnel listens on 8000 (the monolith publish port; the default `[80,443,8080]` missed it). (PR #2894)
- `config.linkerd.io/skip-outbound-ports: "8000"` on the fc-agentd pod (PR #2895): the egress-proxy -> Linkerd-meshed monolith:8000 HUNG in linkerd outbound; skip-mesh makes it a plain in-cluster call (monolith `defaultInboundPolicy: all-unauthenticated` accepts it). **RE-EVALUATE whether this is still needed** once a model actually writes the file - the harness publish is Go `http.Client`, may behave differently than node; if the meshed path works for Go, revert skip-mesh.
- `fc-agent-init.publishArtifact()` (PR #2896): after the harness exits, POSTs `/tmp/artifact.html` to `ARTIFACT_PUBLISH_URL`, logs the exact HTTP status/body/err, honours `$ARTIFACT_ID` (same-id re-publish = hot reload), and carries the URL on `vsockproto.Message.Result` for the Done message.

### Task 3 completion criteria

`dispatch.submit("make a bouncing-ball canvas demo", recipe="artifact", tier="artifact")` -> goose writes `/tmp/artifact.html` -> harness logs `artifact: published url=https://jomcgi.dev/artifact/<id>` -> the artifact renders at that URL. Then a `default`-tier thread must still use Qwen (already true).

## Task 4: Discord gate + Model-B session (NOT STARTED)

Surface map (verified):

- Bot is `discord.Client` (`projects/monolith/chat/bot.py`), `on_message` only, **no CommandTree**. Add `self.tree = discord.app_commands.CommandTree(self)` in `__init__`, register `/goosecracker <prompt>`, and `await self.tree.sync(...)` in `on_ready` (lines ~204-222). discord.py >=2.4 (pyproject).
- **Gate (server-side allowlist only):** `OWNER_DISCORD_USER_ID` does NOT exist yet. Add the field to the `discord-bot` 1Password item (`vaults/k8s-homelab/items/discord-bot`) and wire it as env in `projects/monolith/chart/templates/deployment.yaml` (alongside `DISCORD_BOT_TOKEN`, or as a plain value under `agent.discord` since a user id is not secret). On the interaction AND every in-thread reply: `if interaction.user.id not in {OWNER_ID}: roast` via the qwen path (`chat/summarizer.build_llm_caller()` -> `call_llm(prompt)`, roast prompt style in `chat/changelog.py`), else proceed.
- **Session (Model B = re-run with curated transcript):** on `/goosecracker`, open a Discord thread (`message.create_thread`/`channel.create_thread`; no precedent in repo yet), store the prompt, `dispatch.submit(transcript, recipe="artifact", tier="artifact", discord_thread=<thread id>)`. Keep a per-thread server-side transcript of OWNER messages + goose outputs ONLY (never ambient chatter or the qwen bot's replies). On a gated reply in the thread, append + re-submit the full curated transcript (a NEW thread each time; Model B). Store the transcript in a new `claude_agent.*` table or reuse.
- **Note:** for hot-reload-on-iteration the re-runs must publish the SAME artifact id. Inject a per-thread `ARTIFACT_ID` (see Task 5) derived from the Discord thread id so every re-run hot-reloads the same artifact.

## Task 5: fc-agentd Done -> discord_outbox (PARTLY DONE)

- DONE: `vsockproto.Message.Result` carries the artifact URL on `KindDone` (set by `fc-agent-init.publishArtifact`). (PR #2896)
- TODO: in `fc-agentd` `control.Serve`/`reconcile` `OnDone`, plumb the `Result` (currently `OnDone(threadID, status)` ignores it) -> if the thread has a `discord_thread`, write the result (goose summary + artifact URL) to `chat.discord_outbox` keyed with `channel_id = <discord thread id>` (a thread id is a channel id; `chat/outbox.py:enqueue_message`). The existing bot drain (`run_outbox_drain`) posts it into the thread.
- TODO: inject a per-thread `ARTIFACT_ID` into the guest (artifact tier) = the `discord_thread` (or `thread_id`) so iterations re-publish the same artifact (hot reload). fc-agentd already has `t.DiscordThread`/`t.ThreadID` at Assign; add it to the injected env in `reconcile.startControl` (next to the tier env).

## Cleanup / follow-ups

- Revert the `dumpGuestDiag` diagnostic (branch `chore/goosecracker-guest-diag`) once the model writes files.
- Re-evaluate `skip-outbound-ports: "8000"` (keep only if the meshed path still hangs for the Go publish).
- Delete stray validation artifacts in S3 (`validate-task2`, `svc-test`) - harmless; artifact TTL/lifecycle on `s3://artifacts` is a deferred follow-up (ADR 024).
- One comprehensive code review per merged PR (already the cadence).

## Key references

- Plan: `docs/plans/2026-06-27-goosecracker-discord-artifact-agent.md`; ADR: `docs/decisions/agents/024-...md` (+ 022 substrate, 023 egress swap).
- Merged PRs: #2890 (Task 1), #2891 (Task 2), #2892 (recipe), #2893 (recipe imperative), #2894 (egress port 8000), #2895 (skip-mesh), #2896 (harness publish), #2897 (openrouter provider), guest-diag (diagnostic).
- Substrate: `projects/agent_platform/fc-agentd` (controller), `projects/agent_platform/fc-agent-init` (guest PID 1), `projects/agent_platform/egress-proxy` (sidecar), `projects/agent_platform/harness` (goose image + recipes), `projects/agent_platform/vsockproto` (control protocol).
- Artifact service: `projects/monolith/artifact/` (router/s3), frontend `projects/monolith/frontend/src/routes/public/artifact/[id]/`.
