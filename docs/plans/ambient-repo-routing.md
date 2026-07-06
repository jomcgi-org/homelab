# Plan: unify chat entry points + ACL-scoped repo auto-selection

## Goal

One routing brain behind every Discord entry point. Two behavior changes:

1. **Ambient/mention agent runs auto-select a repo** from the server+user's
   ACL-granted scopes, instead of always running repo-less. Selection is
   inferred by the same LLM call that already decides `needs_agent`, constrained
   to `acl.allowed_scopes(guild, user, "agent")`, and re-checked server-side.
2. **@mention / reply enters the exact same chat loop as ambient**, just
   bypassing the attention gate. No separate inline-only flow. Side effect: a
   mention in a non-ambient channel can now escalate to the agent (today it
   can't).

`/agent` slash command stays as-is (explicit, repo-pinned escape hatch).

## Non-goals

- No change to `/agent` or `/artifact`.
- No change to the goosecracker guest / `start_agent_flow` engine (it already
  takes a `repo`).
- No new ADR (decided: routing-brain consolidation within existing ACL bounds,
  no new privilege since selection is confined to already-granted repos).

## Design

### attention.py: `needs_agent` -> `classify_engagement`

Replace `needs_agent(message) -> bool` with
`classify_engagement(message, allowed_repos: list[str]) -> EngagementDecision`
returning `{needs_agent: bool, repo: str}`.

- Same single LLM call, richer prompt: list `allowed_repos` and ask the model to
  set `repo` ONLY when the task clearly targets that specific repository, else
  `""`.
- Parse JSON; **validate `repo in allowed_repos` else coerce to `""`** (never
  trust the model to stay in-set).
- Fail closed: on any error return `{needs_agent: False, repo: ""}` (fast inline
  reply, no surprise heavy run, no repo).
- Keep a thin `needs_agent` shim only if other callers exist (grep: only
  bot.py). Otherwise replace outright and update tests.

### bot.py `on_message`: single engage decision

```
explicit = should_respond(message, self.user)      # @mention or reply-to-bot
engage = False
if explicit:
    engage = True                                   # bypass the gate
elif is_ambient:
    ... evaluate() + log_decision() (unchanged) ...
    engage = result.engage
if engage:
    scopes = await to_thread(acl.allowed_scopes, guild_id, author_id, "agent")
    decision = await attention.classify_engagement(message, sorted(scopes))
    if decision.needs_agent:
        await self._engage_agent(message, repo=decision.repo)
    else:
        await self._process_message(message, force_respond=True)
    return
await self._process_message(message)                # non-engaged: saves history,
                                                    # should_respond gate = no reply
```

- `log_decision` (attention_decision rows) stays **ambient-only** so the
  improve-ambient signal isn't polluted by explicit mentions.
- The final `_process_message(message)` still runs for non-engaged messages so
  every message is persisted/embedded for history + search (do NOT drop it).

### bot.py `_engage_agent`: accept and enforce a repo

- Signature `_engage_agent(self, message, repo: str = "")`.
- Grant check keys on the SELECTED repo: `is_granted(guild, user, "agent", repo)`
  (repo `""` = the repo-less permission as today). Defense in depth on top of
  the classifier already picking from `allowed_scopes`.
- Pass `repo` through to `start_agent_flow(channel, user, prompt, repo, ...)`.
- Refusal message unchanged (lists allowed scopes).

## Security

Ambient gains no new privilege: `classify_engagement` can only pick a repo in
`allowed_scopes(guild, user, "agent")`, and `_engage_agent` re-checks
`is_granted(..., repo)`. So an ambient/mention run can hydrate exactly the repos
`/agent` could already run in that server, no more. Fail-closed on classify
error => repo-less.

## Tests

- attention: `classify_engagement` returns needs_agent + repo; repo constrained
  to the allowed list; an out-of-set / hallucinated repo coerces to `""`; error
  path fails closed.
- bot on_message: explicit mention bypasses the gate and enters dispatch;
  mention + needs_agent -> `_engage_agent` called with the selected repo; ambient
  still runs the gate; non-engaged message still hits `_process_message`.
- `_engage_agent`: grant check uses the selected repo; ungranted repo refuses.

## Rollout

Single monolith PR + chart bump. No migration. Verify on CI, then a live smoke:
mention Bosun with a repo-targeted ask in a granted server and confirm the run
hydrates the right repo.
