# Agent tooling: reaching the cluster, CI, and Joe

What a Claude session in this repo can actually call, and what it cannot. Read
this before hunting for an MCP tool.

## What is registered

Only two MCP servers are available by default:

- **monolith** (`monolith-*` and `monolith-monolith-agent-*` tools) via Context
  Forge / the `claude_ai_homelab` remote server, prefixed
  `mcp__claude_ai_homelab__`.
- **buildbuddy** (`mcp__buildbuddy__*`) via the project-scoped `.mcp.json`, which
  points at `https://jomcgi.buildbuddy.io/mcp` and needs `${BUILDBUDDY_API_KEY}`
  in the shell env before the session starts. Without that variable the tools do
  not load and there is no fallback path for inspecting CI runs, so set it in
  `~/.zshrc`.

The kubernetes, ArgoCD, and SigNoz MCP servers are **gone**. As of 2026-06-12
Context Forge serves only the `GitHub` and `monolith` gateways (confirmed by
querying its Postgres). Do not spend turns on `ToolSearch +kubernetes`,
`+argocd`, or `+signoz`: those tools are not there. Use `kubectl` instead.

## Where to look for what

| Need | Tool |
|------|------|
| K8s resources | `kubectl get` / `kubectl describe -n <ns>` |
| K8s logs | `kubectl logs -n <ns>` for recent, SigNoz UI for historical |
| K8s metrics | `kubectl top pods -n <ns>`, `kubectl top nodes` |
| ArgoCD apps | `kubectl get applications -n argocd` (add `-o yaml` for sync and health). UI at `private.jomcgi.dev/app/argocd` |
| BuildBuddy CI | `mcp__buildbuddy__get_invocation` (selector `invocationId` or `commitSha`), then `get_target`, `get_action`, `get_log`. `get_file_range` reads byte ranges from CAS blob URIs in build events, 16 MiB max |
| Observability (logs, traces, metrics, dashboards, alerts) | SigNoz UI at `private.jomcgi.dev/app/signoz`. No MCP by default; running `projects/platform/signoz-addons/signoz-mcp-wrapper.sh` locally exposes `signoz-*` tools |
| Agent jobs | `monolith-monolith-agent-list-routine-jobs`, `monolith-monolith-agent-trigger-routine-job`, `monolith-monolith-agent-trigger-job` |
| Message Joe | `monolith-monolith-agent-notify` (see below) |

Remember that `kubectl` here is read-only: `apply`, `patch`, `edit`, `scale`, and
`delete` are forbidden because the cluster is GitOps-managed.

## Messaging Joe

`monolith-monolith-agent-notify` posts a Discord message through the in-process
bot. Arguments: `message`, optional `level` of `info` / `warn` / `error`, and an
optional `channel` from the allow-list (defaults to the homelab channel). It is
outbound only: it cannot read or list channels.

This is how a session reaches Joe: a finished long task, a blocked decision, a
heads-up. Be sparing, one clear message rather than a play-by-play.

**Single voice.** Only the top-level main-loop agent may send this. Subagents
must never Discord-notify Joe: they surface blockers, design forks, and questions
to the dispatching agent (final report or `SendMessage`), and the main agent
decides whether it warrants reaching Joe. That keeps Joe hearing one voice
instead of parallel pings from fan-out workers.

**Ending a turn while blocked.** If you stop a turn waiting on Joe (an unanswered
`AskUserQuestion`, an approval you need, a decision only he can make) and he may
be away from the session, send one notify so the blocker actually reaches him
instead of sitting in a session nobody is watching. One line: what you need and
why you are stopped. Do not notify for routine turn endings with no decision
pending, and do not double-notify the same blocker. A subagent that hits a fork
reports it to its dispatcher instead; the main agent resolves it inline where it
can and escalates only decisions that are genuinely Joe's.
