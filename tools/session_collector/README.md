# Session collector

This cron-run tool uploads finished local Claude Code and Codex sessions to the
private monolith knowledge API. It emits one Markdown raw per session. The
server deduplicates content, while a local JSON state file avoids routine
re-uploads and permits a session to resume when its transcript grows.

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

Authenticate once, then install the launch agent:

```sh
cloudflared access login https://private.jomcgi.dev
tools/session_collector/install.sh
```

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
