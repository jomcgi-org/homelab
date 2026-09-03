# Session collector

This cron-run tool uploads finished local Claude Code and Codex sessions to the
private monolith knowledge API. It emits one Markdown raw per session. The
server deduplicates content, while a local JSON state file avoids routine
re-uploads and permits a session to resume when its transcript grows.
The default state file is
`~/Library/Application Support/homelab/session-collector/state.json`. On first
use, the collector moves an existing state file from the former
`~/.cache/homelab-tools/session-collector/state.json` location.

Before upload, the collector removes model thinking and unsupported records,
caps large fragments and documents, and redacts AWS credentials, GitHub tokens,
OpenAI and Anthropic keys, Slack tokens, JWTs, private key blocks, bearer
headers, URL basic authentication, secret key-value pairs, Cloudflare cookies,
1Password service tokens, and environment dumps. Redaction happens locally.
Transcript content and secret values are never logged.

Inspect a redacted transcript before enabling uploads:

```sh
python3 -m tools.session_collector render ~/.claude/projects/project/session.jsonl
```

Install the launch agent:

```sh
python3 -m venv .venv
.venv/bin/pip install httpx
tools/session_collector/install.sh
```

On every run, the collector reads the local Tailscale status. When Tailscale
reports `BackendState` as `Running`, it sends directly to
`http://monolith.<tailnet>.ts.net` without a cookie. The resolver finds the
Tailscale app binary at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`,
so launchd does not need it on `PATH`.

When Tailscale is not running or its status cannot be read, the same run falls
back to `https://private.jomcgi.dev` with the cached Cloudflare token. If that
token is unavailable or expired, the collector logs the login command and
skips the run. Populate the cache with:

```sh
cloudflared access login https://private.jomcgi.dev
```

`--auth auto` is the default. It selects `none` for `.ts.net` hosts and
`cloudflare` for other hosts. Pass `--auth none` or `--auth cloudflare` to
override that selection. `SESSION_COLLECTOR_BASE_URL` and `--base-url` remain
runtime overrides; the installer does not bake either endpoint into the launch
agent.

The launch agent runs `.venv/bin/python3`. Its output is written to
`~/Library/Logs/session-collector.log`; this log is not rotated automatically.

Pause collection without deleting its state:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/dev.jomcgi.session-collector.plist"
```

Forget a transcript so it is considered for upload again:

```sh
python3 -m tools.session_collector forget /absolute/path/to/session.jsonl
```

Use `python3 -m tools.session_collector status` to inspect state counts, or
`python3 -m tools.session_collector run --dry-run` to list eligible uploads and
their per-class redaction counts.

When a transcript's working directory is gone or has no Git origin, the
collector uses path-prefix mappings for known homelab worktree locations. A
path-prefix match is accepted only when the path is inside a worktree listed by
the allowlisted homelab repository. This invariant prevents an arbitrary
deleted directory under a shared worktree parent from being treated as
homelab. Pass `--allow-path /absolute/prefix=owner/repo` more than once to
replace those defaults with custom mappings. A transcript-provided origin takes
precedence over an origin discovered from an existing checkout, which in turn
takes precedence over path mappings.
