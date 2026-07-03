# Orchestrator Brief-Compiler Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (repo default; one comprehensive code review per merged PR, tests run on CI only).

**Goal:** Implement ADR 036: a host-side brief compiler in the monolith chat harness that routes chat vs goose before microVM boot and compiles a grounded, cache-friendly task brief for escalations, via OpenRouter.

**Architecture:** A new `chat/orchestrator.py` module sits between the ADR 035 shared agent flow and `goosecracker.api.submit()`. It calls OpenRouter through an httpx client shaped like `summarizer.build_llm_caller` (summarizer.py:412), with a deterministic prompt: committed baked bundle + versioned directive as `system`, volatile context as `user`. Output is a strict-JSON brief or a chat verdict; every call logs to `chat.orchestrator_brief`. Everything fails open to today's direct-submit path.

**Tech Stack:** Python (monolith chat module), httpx, OpenRouter (OpenAI-compatible `/v1/chat/completions`), Postgres via Atlas migrations, 1Password Operator for the key, goose recipes on fc-invoke.

**Companion spec:** [2026-07-02-orchestrator-brief-compiler-spec.md](2026-07-02-orchestrator-brief-compiler-spec.md). Rationale: [ADR 036](../decisions/agents/036-orchestrator-brief-compiler-tier.md).

**Assumption:** the ADR 035 plan is complete: `chat/attention.py`, the shared `start_agent_flow`, checklist renderer with stage markers, and steering all exist. If 035's Phase 4 shipped a guest-side `chat.yaml`, it stays as the fail-open route; this plan makes the host-side verdict the primary router.

**Repo rules that override generic practice:**

- No local test runs; CI verifies. New `chat/*_test.py` files need hand-added `py_test` targets in `projects/monolith/BUILD` (copy the `chat_goosecracker_test` shape).
- SQLite fixtures use `create_all`; mirror CHECK constraints in `__table_args__`.
- Migrations: `projects/monolith/chart/migrations/YYYYMMDDhhmmss_<desc>.sql`.
- Monolith deploys need a manual chart bump (`chart/Chart.yaml` + `deploy/application.yaml` together). Recipe changes additionally need an fc-invoke chart bump.

**Phasing = PR boundaries**, sequential.

---

## Phase 1: OpenRouter client + secret plumbing

### Task 1.1: Values + 1Password secret + env wiring

**Files:**

- Create: `projects/monolith/chart/templates/onepassworditem-orchestrator.yaml` (copy the gardener template shape)
- Modify: `projects/monolith/chart/values.yaml` (defaults), `projects/monolith/deploy/values.yaml` (cluster overrides), the deployment template env block
- Modify: `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` (bump at phase end)

**Steps:**

1. Add the `orchestrator.*` values block per the spec's configuration table (`enabled: false` default). Gate the `OnePasswordItem` and the env vars (`OPENROUTER_API_KEY` from the secret; `ORCHESTRATOR_MODEL`, `ORCHESTRATOR_BASE_URL`, `ORCHESTRATOR_TIMEOUT_S` from values) on `orchestrator.enabled`.
2. Render check: `helm template monolith projects/monolith/chart/ -f projects/monolith/deploy/values.yaml` with the flag on and off.
3. Commit: `feat(monolith): orchestrator values and OpenRouter secret plumbing`

### Task 1.2: OpenRouter client module

**Files:**

- Create: `projects/monolith/chat/orchestrator_client.py`
- Test: `projects/monolith/chat/orchestrator_client_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests (httpx mock transport): `call(system, user) -> OrchestratorResponse(content, prompt_tokens, completion_tokens, cached_tokens, latency_ms)`; sends OpenAI-compatible chat payload with pinned model; raises typed `OrchestratorUnavailable` on timeout/HTTP error (no retries beyond one, per fail-open philosophy); reads config from env; returns provider `usage` fields including cached-token counts when present.
2. Implement, mirroring `build_llm_caller`'s shape (summarizer.py:412) but unary and short-timeout.
3. Commit: `feat(chat): OpenRouter orchestrator client`

**Phase 1 gate:** PR, CI, review, merge (no chart bump needed yet; `enabled: false`).

---

## Phase 2: Baked context bundle

### Task 2.1: Bundle generator + committed artifact

**Files:**

- Create: `projects/monolith/knowledge/tools/gen_orchestrator_bundle.py` (beside the manifest generators)
- Create: `projects/monolith/chat/orchestrator_prompt.md` (base prompt source, hand-written)
- Create (generated): `projects/monolith/chat/orchestrator_bundle.md`
- Test: `projects/monolith/chat/orchestrator_bundle_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests: the generator output is byte-deterministic (two runs identical); contains the base prompt, then one line per recipe from `projects/firecracker/goosecracker/guest/recipes/*.yaml` (name + its `description:` field, sorted by name), then the repo structure digest (sorted top-level `projects/` listing + `docs/decisions` category list); no timestamps.
2. Implement following the `gen_repo_docs_manifest.py` conventions (git-index-driven, deterministic). Wire into the same auto-commit/freshness flow the doc manifests use so recipe description changes refresh the bundle in CI.
3. Commit: `feat(chat): deterministic orchestrator context bundle`

**Phase 2 gate:** PR, CI, review, merge.

---

## Phase 3: Orchestrator module, consent gate, telemetry

### Task 3.1: Brief schema + telemetry table

**Files:**

- Create: `projects/monolith/chart/migrations/<ts>_chat_orchestrator_brief.sql`
- Modify: `projects/monolith/chat/models.py`
- Test: extend `projects/monolith/chat/models_db_constraints_test.py`

**Steps:**

1. Migration + SQLModel for `chat.orchestrator_brief` exactly per the spec's telemetry table (CHECK: `route IN ('chat','goose','failopen')`, mirrored in `__table_args__`).
2. Commit: `feat(chat): orchestrator brief telemetry table`

### Task 3.2: Prompt assembler + brief parsing

**Files:**

- Create: `projects/monolith/chat/orchestrator.py`
- Test: `projects/monolith/chat/orchestrator_test.py` (new; register in BUILD)

**Steps:**

1. Write failing tests for `assemble_prompt(bundle, directive, kg_results, channel_context, request) -> (system, user)`: stability order per spec section 4; identical inputs give identical bytes; directive rendered with version tag; volatile content only in `user`.
2. Write failing tests for `parse_brief(content) -> Brief | ChatVerdict`: strict JSON schema from spec section 3; unknown keys tolerated, missing required keys = parse failure; `repo` outside invoker scopes replaced by invoker scope (function takes the allowed-scopes set) with a flag set for logging.
3. Write failing tests for `compile(request_ctx) -> Verdict`: consent grant (`acl.is_granted(guild, "", "orchestrator", channel)`) and `orchestrator.enabled` checked first (no client call otherwise); client errors, timeout, and parse failures return `FailOpen(reason)`; every path writes exactly one `chat.orchestrator_brief` row with the right `route`.
4. Implement.
5. Commit: `feat(chat): orchestrator brief compiler with consent gate and fail-open`

### Task 3.3: Wire into the agent flow

**Files:**

- Modify: `projects/monolith/chat/bot.py` (the ADR 035 shared `start_agent_flow`)
- Test: extend the 035 flow tests (`bot_on_message_test.py` or the flow test file 035 created)

**Steps:**

1. Write failing tests: `chat` verdict → conversational reply path, no session, no checklist; `goose` verdict → checklist message pre-rendered from `brief.stages`, submit called with the brief-rendered task input (brief markdown + raw prompt); `FailOpen` → exact pre-036 behaviour.
2. Implement; the checklist pre-render reuses the 035 `render_checklist` on a synthetic all-pending stage list.
3. Commit: `feat(chat): route escalations through the orchestrator`

**Phase 3 gate:** PR, CI, review, merge, chart bump with `orchestrator.enabled: true` for the home server plus the 1Password item path. Live check: granted channel gets brief-driven sessions; revoking the grant restores direct submit.

---

## Phase 4: Guest handoff

### Task 4.1: Router respects a provided brief

**Files:**

- Modify: `projects/firecracker/goosecracker/guest/recipes/agent.yaml`
- Modify: fc-invoke chart bump

**Steps:**

1. When the task input carries a brief header (route + stage plan), the router skips its own classification, adopts the provided route and stage plan (emitting the matching `::stages::` announcement so host and guest agree), and treats `hints`/`constraints`/`done_criteria` as context. Without a brief header, behaviour is unchanged (fail-open compatibility, and the 035-built guest routing stays the degraded path).
2. Bump fc-invoke chart.
3. Commit: `feat(goosecracker): adopt host-compiled briefs in the recipe router`

**Phase 4 gate:** PR, CI, review, merge, fc-invoke bump. End-state check: run the spec's acceptance list top to bottom (ungranted silence, fail-open on outage, pre-rendered checklist, out-of-scope repo discarded, identical system bytes across consecutive escalations, telemetry rows for every route).
