# EmberVM claude runtime

Session-class guest image that runs the Claude Code CLI headlessly under a shim
speaking the frozen HTTP-over-vsock guest contract on port 1027. Part of issue
[#4055](https://github.com/jomcgi/homelab/issues/4055).

## Why the CLI and not the API

`claude -p --input-format stream-json --output-format stream-json --verbose` is a
full Claude Code session, not a reduced mode: the complete tool surface,
subagents, skills, and slash commands are all present. The only thing `--print`
removes is the terminal UI. Input is JSONL on stdin, output is JSONL events on
stdout, and the session it creates is the same on-disk object an interactive
`claude --resume <id>` can later attach to.

`--verbose` is mandatory with that flag combination; without it the CLI exits with
`Error: When using --print, --output-format=stream-json requires --verbose`.

## Turn model

One HTTP request over vsock drives one agent turn. The CLI process is long-lived
and owned by the shim, so it stays warm between turns:

| | measured |
| --- | --- |
| Binary boot (`claude --version`) | ~65ms |
| Cold spawn to first request | ~250ms (~167ms with no MCP servers) |
| Warm turn on a live process | 77-98ms |
| API leg | 1-5s, varies ~3x run to run |

The API leg dominates by an order of magnitude and is noisy enough that the local
overhead is not worth optimising further. In particular this is why the guest does
not need memory-snapshot warmth: relighting from a cold boot costs a quarter
second against a multi-second turn.

## CLI prewarm

The shim can park every CLI family at boot so the first turn adopts a live
process instead of paying its spawn (`#4423`). Which CLIs to prewarm ships as
`/usr/share/ember-shim/prewarm-clis`, baked into each image: this image lists
`claude,codex,pi`; the pi-only image (`../pi`) lists just `pi`, because
prewarming a binary it omits would fail the boot. The list can be overridden
per boot with `EMBER_PREWARM_CLIS`. It cannot be delivered through workload
`initEnv`: those entries never reach a guest environment (`#4429`), which is
why earlier attempts silently no-oped and every turn logged `path=lazy_spawn`.

Prewarm parks each family differently:

- **claude** spawns and then clears the generated session id: a parked process
  owns no caller session until its first user message.
- **codex** binds nothing at spawn. Thread identity, model, effort and developer
  instructions all ride per-turn requests, so the initialize handshake is the
  whole init cost.
- **pi** takes model and system prompt as spawn flags, so it parks on the
  default model with no caller prompt. A different model costs one `set_model`
  RPC; a different prompt respawns through the turn path.

A turn that hands a session id to an unbound process reports `path=adopt` in
the turn-timing log; a turn served by the already-bound process reports
`reuse`. During a base build `/shim/ready` stays 503 until every configured
parked CLI is alive, so the snapshot cannot capture warmth that is not there.

## Completion and metering

The `result` event carries the completion signal and the usage record together:
`terminal_reason`, `stop_reason`, `is_error`, `permission_denials`, `num_turns`,
`session_id`, `usage`, `total_cost_usd`, `modelUsage`. There is no window where a
turn has finished but is unaccounted for.

Two cautions when consuming it:

- `duration_api_ms` appears to be **cumulative** across a live process, not
  per-turn. Do not sum it per turn.
- `total_cost_usd` is the API-rate equivalent. On a subscription credential
  nothing is charged at that rate, so it is a valid unit of account for internal
  quota but is not an invoice.

## Preemption

SIGINT mid-turn exits 0, persists the partial output, and appends a synthetic
`[Request interrupted by user]` user turn so the transcript stays a structurally
valid conversation (no dangling `tool_use` without its `tool_result`). A resumed
session then has full context, and a bare `continue` picks up exactly where it
stopped. So the drain path is: SIGINT, await exit 0, bank.

The shim gives SIGINT up to 30 seconds to unwind a Bash tool and write the
synthetic interrupt turn. SIGKILL is only a logged last-resort backstop after
that timeout, because it can leave a truncated final JSONL line. Initialization
has a 15-second deadline; an output timeout follows the same SIGINT-first path.

Turn output has a 600-second deadline, and it is worth being precise about what
that measures. It is a per-event inactivity timer, not a cap on total turn
duration: it resets on every stream event, so a turn producing steady output can
run far longer. It is sized to span a single silent tool call, because the CLI
emits nothing to stdout while a Bash tool executes, so the bound has to exceed
the slowest realistic in-guest command (a build or a test run) rather than the
slowest turn. Its job is spotting a genuinely wedged CLI. Total turn duration is
bounded separately by the caller (`read_timeout` in
`projects/monolith/agent_sessions/transport.py`), and this value must stay
comfortably below that one so the inner watchdog fires first and reports a
specific error instead of the caller timing out generically.

The shim owns one Claude session. A supplied `session_id` must match the active
session or the turn receives HTTP 409. If no id is supplied after an interrupt,
the shim resumes the last known session id instead of silently starting a new
conversation.

## Image contents and the arch decision

The CLI ships as a single **262 MB** arch-specific ELF, dynamically linked against
glibc (`/lib64/ld-linux-x86-64.so.2`, GLIBC_2.2.5 baseline), fetched from the npm
registry by the `claude_code_cli` entry in `MODULE.bazel`.

This image is **amd64 only**. Every cluster node is amd64 and so is the RBE build
executor, so a dual-arch manifest would fetch a second 262 MB payload that no node
can run. Same reasoning as the control-plane image. To add arm64 later, set
`arm64_url` / `arm64_sha256` on `claude_code_cli` and add `aarch64` to
`apko.yaml` together.

`glibc` is pinned explicitly rather than left to arrive transitively, since the
CLI hard-requires the loader and a changed transitive edge would break the guest
silently.

## Guest configuration that is easy to miss

- **Workspace trust** is pre-seeded at `/home/runtime/.claude.json` keyed by the
  absolute workspace path. Without it a fresh guest wedges, because the trust
  dialog is interactive and nobody is at a keyboard.
- **`bash`**, not just busybox `sh`: the Bash tool assumes bash.
- **`git` plus a committer identity.** The identity reaches the guest through
  `ember.env.*` boot arguments, decoded by guest-init before the shim starts, and
  the shim configures Git when it spawns or resumes the CLI, so a session can
  carry its principal without an identity baked into the image. Note that the
  identity is plumbed but nothing commits with it yet: per-session branches,
  per-turn commits, and a diff endpoint are tracked in
  [#4070](https://github.com/jomcgi/homelab/issues/4070). Until that lands there
  is no record of what a session changed, which is the main gap in this runtime.
- **`HOME` must be writable** by uid 65532: the CLI writes `~/.claude.json` and
  session transcripts under `~/.claude/projects/`.

## Open items

- **No record of what a session changed.** Per-session branches, per-turn
  commits, and a diff endpoint are tracked in
  [#4070](https://github.com/jomcgi/homelab/issues/4070). The git identity is
  already plumbed; nothing uses it yet.
- **Regenerating `apko.lock.json` needs Linux.** apko will not run on darwin
  (`cannot execute binary file`), so relocking is a podman job. There is also a
  bootstrap order to respect: the `apko.translate_lock` entry in `MODULE.bazel`
  reads the lock, so a from-scratch regeneration means removing that entry,
  generating, then restoring it.
