# ADR 029: Discord Bot Feature ACL (per-server command and repo grants)

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-02
**Builds on:** [024 - Discord Agent, Hosted-Model Tiers, and Artifacts](024-discord-agent-hosted-model-tiers-and-artifacts.md) (the `/agent` and `/artifact` commands and the owner gate this replaces), [041 - Hot Git Mirror Agent Workspaces](041-hot-git-mirror-agent-workspaces.md) (the mirror that serves the per-server repo, including private `loom`)

---

## Problem

`/agent` and `/artifact` are gated by a single check: `is_owner(user_id)`, an exact match against one `OWNER_DISCORD_USER_ID`, fails closed. That was right when the only user was the owner, but two things changed:

1. **We want to open `/agent` to other people.** A collaborator on the Loom server should be able to run the coding agent. The homelab repo is public and the agent runs on the free in-cluster qwen in a sandboxed microVM, so there is no privacy or cost reason to keep `/agent` owner-only.

2. **The repo the agent operates on is a user-picked `Choice` (homelab | loom) with nothing binding it to the Discord server.** `loom` is a **private** repo (weave-hand org, 50:50 with a collaborator). If access widens without binding repo to server, anyone allowed anywhere could select `loom`. Private-repo isolation must be a property of the system, not of who remembers not to pick the wrong repo.

`/artifact` is different: it hosts a public page and spends model budget, so it stays restricted until it has its own safeguards (signed URLs, qwen), out of scope here.

Two more forces shape the design. The set of servers and who can do what will change over time, so the policy must be **editable at runtime**, not a chart redeploy per change. And this should not be a one-off for `/agent`: the next command will want the same gating, so the model should be **generic over features**.

## Decision

**1. A single generic grant table, `discord_feature_grant`, is the authority for every gated bot command.** A grant is a row `(guild_id, subject_id, feature, scope)`:

- `guild_id` — the Discord server. `""` means any server (a global grant).
- `subject_id` — a Discord user id, or `""` for everyone in that server.
- `feature` — the command/capability key: `agent`, `artifact`, and whatever comes next.
- `scope` — a feature parameter, or `""` for the whole feature. For `agent`, scope is the repo name.

Access is **allow-list only** (no deny rules): an action is permitted iff a matching grant exists. Three helpers express every gate: `feature_enabled(guild, feature)`, `allowed_scopes(guild, user, feature)` (which drives the repo validation), and `is_granted(guild, user, feature, scope)`. Empty-string sentinels rather than SQL NULLs keep the composite key clean and behave identically under the SQLite test fixtures and Postgres.

Rationale: one table gates all commands, per-server, optionally per-user, optionally per-scope, with no new schema when a feature is added. It is the smallest model that expresses "everyone in this server may run agent on these repos" and "only the owner may run artifact here" at once.

**2. `/agent` opens to everyone in an opted-in server, and the repo is bound to the server, not chosen freely.** A server is opted in by having `agent` grants; a server with none gets no `/agent` (fails closed). The requested repo must be in `allowed_scopes(guild, user, "agent")`, enforced server-side. A Loom-server run can only ever hydrate `loom`; it cannot request `homelab`, and a homelab-server run cannot reach `loom` unless that server is granted it. Because the guest is already egress-sandboxed and only ever clones the repo the runner hands it, binding the repo to the server's grant set **is** the private-repo isolation.

**3. `/artifact` stays owner-gated, now expressed as a grant** (`artifact` for the owner on the home server) rather than a hardcoded `is_owner`. Same mechanism, no special case. Widening artifact later is adding rows plus its own safeguards, not new code.

**4. Grants live in Postgres and are seeded, not baked into config.** An idempotent startup bootstrap ensures the home-server and owner defaults exist, read from the existing `MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID` and `OWNER_DISCORD_USER_ID` env, so the home server always works out of the box with no manual step. Every other server (Loom) is opted in by inserting rows, at first via SQL or an MCP tool, later via an owner-only management command. Runtime edits take effect on the next command with no redeploy.

## Consequences

- The owner env vars stay, but only as the **seed source** for defaults; the runtime authority is the table. An operator can revoke or extend access by editing rows.
- `is_owner` remains for the artifact gate and for artifact-thread replies until artifact moves fully onto grants.
- Isolation is only as good as the grant data: a wrong row (granting `loom` to the wrong server) leaks the private repo. The seed is conservative (home + owner only); widening is a deliberate insert.
- Deny/precedence rules and role-based subjects are intentionally omitted; if a future feature needs "everyone except X", revisit then rather than carrying an unused deny path now.

## Live target for the first rollout

- Home server (`MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID`): `agent` on the public `homelab` for everyone in the server; `agent` on the private `loom` for the owner only; `artifact` for the owner. (Loom is scoped to the owner here so a home-server member cannot drive the private repo; loom-server members get loom via the grant below.)
- Loom server (`1512814732392927463`): `agent` on `loom` for its members.
