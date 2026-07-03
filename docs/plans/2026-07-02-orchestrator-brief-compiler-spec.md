# Orchestrator Brief-Compiler: Behavioural Spec

**Decision record:** [ADR 036](../decisions/agents/036-orchestrator-brief-compiler-tier.md) (Accepted)
**Companion plan:** [2026-07-02-orchestrator-brief-compiler.md](2026-07-02-orchestrator-brief-compiler.md)
**Assumes:** the ADR 035 plan is implemented (attention gate, shared agent flow, checklist renderer, stage markers, steering).

---

## 1. When the orchestrator runs

- Only on **escalation candidates**: a trigger that passed the attention gate and is not already known to be chat-only. Attention classification and plain conversational replies never call it.
- Only when the guild/channel has the **consent grant** (ADR 029 `DiscordFeatureGrant`, `feature="orchestrator"`) and `OPENROUTER_API_KEY` is configured.
- Otherwise the flow is byte-for-byte today's behaviour: direct `submit()` with the raw prompt, depth routing done by the guest recipe router. This fail-open path is permanent, not transitional; any channel can be returned to it by revoking the grant.

**Acceptance.**

- Ungranted channel: no OpenRouter traffic ever (no request logged, no key read on the path).
- Granted channel with the API unreachable: session still starts within the fail-open timeout; a warn-level log records the degradation.

## 2. The orchestrator call

One call per escalation, two possible outcomes:

- **`route: chat`** — the request is conversational. No microVM boots. The call returns **reply guidance** (the `reply_guidance` block in the schema below); the reply itself is written by the existing local-Qwen concierge, seeded with that guidance as extra context. The guidance persists in telemetry.
- **`route: goose`** — the call returns a **brief** (schema below), which becomes the session's task input.

**Timeout:** 10s (env `ORCHESTRATOR_TIMEOUT_S`). Timeout, HTTP error, or unparseable output all fail open to direct submit.

**Misroute recovery:** a `chat` verdict that turns out to need real work follows the ADR 035 spec's escape hatch (reply says so, escalates once). A `goose` verdict on a trivial question just produces a fast session; no special handling.

## 3. Brief contract

The orchestrator returns strict JSON (schema-enforced client-side; invalid JSON = fail open):

```json
{
  "route": "chat | goose",

  "//goose": "the fields below are set on the goose route, null/absent on chat",
  "recipe": "query | research | plan | implement | artifact",
  "repo": "<repo scope or empty>",
  "repo_paths": ["projects/monolith/chat/bot.py"],
  "hints": "prose: relevant structures, prior art, gotchas",
  "constraints": "prose: what must not change",
  "done_criteria": ["checkable statement", "..."],
  "stages": [{ "title": "..." }],

  "//chat": "the field below is set on the chat route, null/absent on goose",
  "reply_guidance": {
    "context": "prose: what retrieval surfaced that the reply should use",
    "redirect": "prose or null: a suggested steer, e.g. offer to escalate to a repo task",
    "direction": "prose: how to shape the reply (length, tone, what to point at)"
  }
}
```

- Fields partition by `route`: the goose route sets the brief fields and leaves `reply_guidance` null/absent; the chat route sets `reply_guidance` and leaves the brief fields null/absent. The parser validates the block that matches `route` and tolerates the other being missing; a route whose matching block fails validation is a parse failure (fail open).
- `stages` pre-renders the ADR 035 checklist immediately (before guest boot); the guest's own `::stages::` announcement replaces it if the plan changes.
- The brief is **advisory**: the guest may re-route or re-plan; ACLs, recipes, and the egress boundary constrain execution exactly as without a brief. `repo` must be within the invoker's ADR 029 scopes or the brief's repo is discarded in favour of the invoker-selected one.
- `reply_guidance` is **advisory** too: the local Qwen concierge stays the author of the reply and may ignore a redirect or direction that does not fit. It shapes the reply, it never takes an action, never escalates on its own, and cannot widen scope.
- The compiled task input the guest receives is the brief rendered as markdown plus the raw user prompt. The compiled reply input the concierge receives is the raw user prompt plus `reply_guidance` as supplementary context. In both cases the user's words are ground truth; the orchestrator output is never sent alone.

**Acceptance.**

- A goose-routed request shows a checklist message populated from `stages` before the session's first stage marker arrives.
- A brief naming a repo outside the invoker's grants results in the invoker's own repo scope being used, and the discrepancy logged.
- A chat-routed escalation produces a concierge reply (local Qwen) written with `reply_guidance` in context, and that guidance is recorded in telemetry.
- A chat-route `redirect` suggestion does not force an escalation: the concierge may decline it, and no session is submitted unless the ADR 035 escape hatch fires on a later turn.

## 4. Prompt assembly and caching

Assembled in strict stability order, byte-deterministic:

1. **Baked bundle** (committed artifact, generated from repo sources): base orchestrator prompt + recipe catalog (name + one-line description per recipe YAML) + repo structure digest. Regenerated by the format/auto-commit tooling when inputs change, like the docs manifests.
2. **Channel directive**, referenced with its version number.
3. **Volatile tail**: KG search results, channel context window, the request.

No timestamps, ids, or unsorted collections anywhere before the volatile tail. The OpenRouter request sets the bundle+directive as the `system` message and the volatile tail as the `user` message, so provider prefix caching applies to the stable portion.

**Acceptance.**

- Two consecutive escalations in the same channel produce identical `system` message bytes (asserted in tests on the assembler, not the network).
- Telemetry records provider-reported cached token counts when present, so cache hit rate is observable.

## 5. Telemetry

Every orchestrator call writes one row to `chat.orchestrator_brief`:

| Column                                                | Notes                                                                                                                   |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `id`, `created_at`                                    |                                                                                                                         |
| `thread_id`                                           | Links to `goosecracker_sessions` (null for `chat` routes with no thread)                                                |
| `model`                                               | OpenRouter model id used                                                                                                |
| `route`                                               | `chat`/`goose`/`failopen`                                                                                               |
| `brief_json`                                          | Full orchestrator output: the brief on the goose route, the `reply_guidance` block on the chat route (null on failopen) |
| `directive_version`                                   | Directive the call ran under                                                                                            |
| `latency_ms`                                          |                                                                                                                         |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | From the provider response when available                                                                               |
| `error`                                               | Populated on failopen (timeout, HTTP status, parse)                                                                     |

This is the substrate for brief-vs-execution attribution and the future ADR 037 loop. No separate spend dashboard in scope; token columns are enough to compute cost.

## 6. Configuration surface

| Values key                          | Purpose                                                        |
| ----------------------------------- | -------------------------------------------------------------- |
| `orchestrator.enabled`              | Master switch (default false until rollout)                    |
| `orchestrator.onepassword.itemPath` | 1Password item holding the OpenRouter key                      |
| `orchestrator.model`                | e.g. `deepseek/deepseek-chat-v4-flash` (pinned, never `:auto`) |
| `orchestrator.baseUrl`              | `https://openrouter.ai/api/v1` (overridable for testing)       |
| `orchestrator.timeoutSeconds`       | Default 10                                                     |

The key is host-side only: it is never in guest values, the swap catalog, or any surface that crosses the ADR 023 egress boundary.

## Out of scope

- ADR 037 (telemetry judging loop, frontier reruns, preference data): consumes the rows this creates.
- Per-user orchestrator budgets/quotas.
- Streaming the brief; the call is small enough to be unary.
