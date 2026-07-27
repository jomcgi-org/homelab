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
that timeout, because it can leave a truncated final JSONL line. Normal turn
output has a 60-second deadline and initialization has a 15-second deadline;
an output timeout follows the same SIGINT-first path.

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
- **`git` plus a committer identity**, or the per-turn commits fail. The identity
  reaches the guest through `ember.env.*` boot arguments, decoded by guest-init
  before the shim starts. The shim configures Git when it spawns or resumes the
  CLI, so it can carry the session principal without baking an identity.
- **`HOME` must be writable** by uid 65532: the CLI writes `~/.claude.json` and
  session transcripts under `~/.claude/projects/`.

## Open items

- `apko.lock.json` must be generated before the image builds.
