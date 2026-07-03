# DeepSeek Runtime-Constructed Goose Recipes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (per repo CLAUDE.md, plan execution defaults to subagent-driven; do not offer the two-option handoff).

**Goal:** Let the DeepSeek orchestrator construct a goose recipe at runtime in Python (select/sequence sub-recipes with per-step context) instead of always shipping the full baked router, with a capped replan escape hatch, so router iteration stops rolling the guest image and delegation gets more targeted.

**Architecture:** The orchestrator emits a **typed `submit_plan` tool call** (validated against a catalog-derived enum, so it selects sub-recipes but never authors YAML). Deterministic Python renders that plan into a router recipe that specializes the proven `agent.yaml` scaffolding: only the enabled sub-recipes are listed (pointing at their stable baked `/home/goose-agent/recipes/<name>.yaml` paths) and the routing preamble is replaced by DeepSeek's explicit ordered step sequence. The router ships to the guest via the existing ADR-040 `injectedContext` channel with `recipe=/injected-context/router.yaml` (zero guest Go change). goose can request a replan through a structured field the router emits; the monolith re-invokes the orchestrator up to 3 times. Any failure (DeepSeek unavailable, invalid plan, replan budget exhausted) falls open to today's baked `recipe=agent` path.

**Tech Stack:** Python 3.12 (monolith `chat/` + `goosecracker/`), `httpx` (OpenRouter client), goose recipe YAML, Helm chart values, aspect_rules_py test targets. No guest image roll. Model: `deepseek/deepseek-v4-flash` (also fixes the invalid `deepseek-chat-v4-flash` id currently pinned).

**Verification:** No local test loop (CLAUDE.md). Each task writes tests + implementation and commits; test **execution** is deferred to end-of-plan BuildBuddy CI on the pushed branch. New `*_test.py` files need a hand-added `py_test` in the package BUILD (see reference in CLAUDE.md memory: gazelle does not generate monolith pytest targets).

**Single PR / churn:** All tasks land on branch `feat/deepseek-runtime-recipes` in one PR. One chart version bump at the end covers both the model-id fix and the feature.

---

## Design invariants (do not violate)

1. **Selection, not authorship.** DeepSeek picks sub-recipe ids from the catalog enum and orders them with context. It never emits recipe YAML. Python renders all YAML.
2. **Fallback is always reachable.** Any error path returns the baked `recipe="agent"` behavior. The feature can never make a session worse than today.
3. **Zero guest change.** Delivery reuses `injectedContext` + verbatim `--recipe <path>`. The generated router lists baked sub-recipe paths in its own `sub_recipes:` block (goose's `delegate` resolves against the recipe's local list, so this is required and sufficient).
4. **The generated router is a specialization of `agent.yaml`,** not a novel recipe: keep progress markers, steering, `recipe__final_output`, `max_turns`. Only the `sub_recipes` list, the routing/plan section, and the response schema's optional `replan` field are parametrized.
5. **The monolith↔guest recipe-path coupling is guarded.** The monolith hardcodes `/home/goose-agent/recipes/<name>.yaml`; a drift-guard test asserts those paths + the sub-recipe id set match the checked-in guest recipes.

---

## Task 1: Fix the invalid orchestrator model id

**Files:**

- Modify: `projects/monolith/chart/values.yaml:642`

**Step 1:** Change `model: "deepseek/deepseek-chat-v4-flash"` to `model: "deepseek/deepseek-v4-flash"` (verified against the live OpenRouter catalog; the `-chat-` form 400s as invalid, silently fail-opening the orchestrator on every escalation in prod today).

**Step 2:** Commit.

```bash
git add projects/monolith/chart/values.yaml
git commit -m "fix(chat): correct invalid orchestrator model id to deepseek-v4-flash"
```

(Chart version bump is deferred to Task 9 so the PR carries one bump.)

---

## Task 2: Recipe catalog manifest (single source of truth)

The manifest is the one place that knows the sub-recipe id set, human descriptions (for the tool schema), and baked guest paths. It generates the `submit_plan` enum and feeds the renderer.

**Files:**

- Create: `projects/monolith/goosecracker/recipe_catalog.py`
- Create: `projects/monolith/goosecracker/tests/recipe_catalog_test.py`
- Modify: `projects/monolith/goosecracker/tests/BUILD` (add `py_test`)

**Step 1: Write the failing test** — assert the manifest is a non-empty, ordered mapping of the six selectable sub-recipes (the router `agent` itself is not selectable), each with a baked path under `/home/goose-agent/recipes/`, AND a drift guard that reads the real guest recipe dir and asserts the manifest ids exactly match the on-disk `*.yaml` basenames minus `agent`.

```python
from pathlib import Path
from goosecracker import recipe_catalog

_GUEST_RECIPES = Path(__file__).parents[3] / "firecracker/goosecracker/guest/recipes"

def test_manifest_ids_and_paths():
    cat = recipe_catalog.CATALOG
    assert list(cat) == ["query", "research", "plan", "implement",
                         "artifact-build", "artifact-review"]
    for rid, entry in cat.items():
        assert entry.baked_path == f"/home/goose-agent/recipes/{rid}.yaml"
        assert entry.description  # non-empty, used in the tool schema

def test_manifest_matches_guest_recipes_on_disk():
    # Drift guard: the monolith manifest must track the baked guest recipes.
    on_disk = {p.stem for p in _GUEST_RECIPES.glob("*.yaml")} - {"agent"}
    assert set(recipe_catalog.CATALOG) == on_disk
```

**Step 2: Implement** — a frozen dataclass `RecipeEntry(id, description, baked_path)` and an ordered `CATALOG: dict[str, RecipeEntry]`. Descriptions are curated one-liners (what each sub-recipe delivers), sourced from the guest recipe `description:` fields. Provide `enabled_enum() -> list[str]` returning `list(CATALOG)`.

**Step 3: Register the py_test in BUILD** (hand-added; gazelle will not generate it). **Step 4: Commit.**

```bash
git commit -m "feat(goosecracker): recipe catalog manifest with guest drift guard"
```

---

## Task 3: Typed Plan schema + submit_plan tool client

**Files:**

- Create: `projects/monolith/chat/orchestrator_plan.py` (Plan dataclass + JSON schema builder + semantic validator)
- Modify: `projects/monolith/chat/orchestrator_client.py` (add `call_tool`)
- Create/Modify: `projects/monolith/chat/orchestrator_plan_test.py`, `projects/monolith/chat/orchestrator_client_test.py`
- Modify: `projects/monolith/chat/BUILD`

**Step 1: Plan schema.** Define:

```python
@dataclass(frozen=True)
class PlanStep:
    sub_recipe: str
    context: str

@dataclass(frozen=True)
class Plan:
    enabled_subrecipes: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    done_criteria: tuple[str, ...]
```

and `submit_plan_schema() -> dict` building the JSON Schema from `recipe_catalog.enabled_enum()` (both `enabled_subrecipes.items.enum` and `steps.items.sub_recipe.enum` reference it — so a non-catalog id is unrepresentable). Mirror the probe schema in `scratchpad/probe_submit_plan.py` (validated 12/12).

**Step 2: Semantic validator** — `validate_plan(plan) -> list[str]`: steps non-empty, every `sub_recipe`/`enabled_subrecipe` in the catalog, every step `context` non-empty, and every stepped sub_recipe is in `enabled_subrecipes`. Returns error strings; a non-empty list forces fail-open in Task 5. (Typed removes structural errors; this catches semantic ones.)

**Step 3: `call_tool` client.** Add to `orchestrator_client.py` a sibling of `call()` that POSTs `tools=[{type:function, function:{name:"submit_plan", parameters: schema}}]` + `tool_choice={type:function, function:{name:"submit_plan"}}`, parses `choices[0].message.tool_calls[0].function.arguments` (JSON), and raises `OrchestratorUnavailable` on any transport/parse error. Accept a `timeout_s` arg (callers pass 60 or 120). Keep the single-attempt, fail-open contract. Document `response_format: json_schema` as the drop-in fallback mechanism in the module docstring (probe showed both 12/12).

**Step 4: Tests** — schema enum contains exactly the catalog ids; `validate_plan` rejects empty steps / empty context / non-catalog id / stepped-but-disabled recipe; `call_tool` parses a mocked tool_call and raises `OrchestratorUnavailable` on HTTP 400 and on a missing `tool_calls`. **Step 5: BUILD + Commit.**

```bash
git commit -m "feat(chat): typed submit_plan tool schema, client, and validator"
```

---

## Task 4: Python router renderer (specialize agent.yaml)

Render a `Plan` into a router recipe: the `agent.yaml` scaffolding with a parametrized `sub_recipes` list and an explicit ordered step section replacing the classification preamble, plus an optional `replan` output field.

**Files:**

- Create: `projects/monolith/goosecracker/router_render.py`
- Create: `projects/monolith/goosecracker/tests/router_render_test.py`
- Modify: `projects/monolith/goosecracker/tests/BUILD`

**Step 1: Failing tests** — `render_router(plan) -> str` returns valid YAML (parseable by `yaml.safe_load`) where:

- `sub_recipes` contains exactly `plan.enabled_subrecipes`, each with `path == /home/goose-agent/recipes/<id>.yaml` and the standard `values` (`task_file`, `context_file`).
- The instructions contain the ordered steps in sequence (sub-recipe id, stage title) and instruct the model to `delegate(source: <id>)` per step rather than classify. Per-step `context` is NOT embedded in the instructions; the model is told to read it from the injected plan file instead.
- `response.json_schema.properties` includes an optional `replan` object (`reason`, `what_i_learned`, `suggested_focus`) and retains `summary`/`mode`.
- `settings.max_turns` and the progress-marker/steering instructions are preserved.
- A disabled sub-recipe id never appears anywhere in the output.
- `render_plan_file(plan) -> str` returns plain markdown, never templated, with one `## Step <i>: <sub_recipe>` section per step holding that step's `context` verbatim, braces and all.

**Step 2: Implement** — keep a monolith-side router **scaffold template** (the shared, non-routing instruction blocks: injected-context handling, progress markers, steering, final_output conventions, brevity) as a module constant derived from `agent.yaml`. Build `sub_recipes` and the step section programmatically from the `Plan`, using only controlled strings (ids, order, titles). Emit YAML via `yaml.safe_dump` (or an explicit builder). Per-step `context` is untrusted and orchestrator-authored (derived from user Discord messages), and goose templates the whole recipe through minijinja BEFORE parsing (`agent.yaml:223-230`), so any `{{ }}`/`{% %}` in that context would be interpreted rather than treated as data if it were embedded in the recipe, never mind the block-scalar/newline breakage. So `context` is never string-concatenated (or safe_dump'd) into the recipe at all: `render_plan_file(plan)` instead renders it into a separate plain-markdown file delivered to the guest at `/injected-context/plan.md` (Task 6), which goose only reads as data, never templates. The recipe instructs the model to read each step's section out of that file and append it to `/tmp/goose/context.md` before delegating.

**Step 3: Drift guard test** — assert the scaffold template's preserved invariants (progress-marker forms, `recipe__final_output` tool name, steering URL var) still appear verbatim in the checked-in `agent.yaml`, so guest convention changes fail this test loudly. **Step 4: BUILD + Commit.**

```bash
git commit -m "feat(goosecracker): render runtime router from a typed plan"
```

---

## Task 5: Orchestrator produces a Plan verdict; wire into compile

**Files:**

- Modify: `projects/monolith/chat/orchestrator.py` (new `PlanVerdict`, extend `compile`)
- Modify: `projects/monolith/chat/orchestrator_bundle.md` (instruct the plan/tool route)
- Modify: `projects/monolith/chat/orchestrator_test.py`

**Step 1: Failing tests** — `compile` on a goose-route escalation returns a new `PlanVerdict(plan: Plan, repo, repo_paths, repo_replaced)`; an invalid/empty plan or `OrchestratorUnavailable` returns `FailOpen`; the existing `ChatVerdict`/`FailOpen`/consent-gate/telemetry-row-exactly-once behaviors are unchanged.

**Step 2: Implement** — add `PlanVerdict` to the `Verdict` union. In `compile`, for a granted goose escalation call `orchestrator_client.call_tool(system, user, schema=submit_plan_schema(), timeout_s=60)`, deserialize into `Plan`, run `validate_plan`; on any error log + `FailOpen`. Keep the repo-scope replacement logic (out-of-scope repo → invoker scope, set `repo_replaced`). Write the one telemetry row (add plan step count / latency fields). Update `orchestrator_bundle.md` to describe producing a minimal ordered plan via `submit_plan` (keep the stable-prefix caching discipline).

**Step 3: Commit.**

```bash
git commit -m "feat(chat): orchestrator emits a validated runtime plan verdict"
```

---

## Task 6: Deliver the rendered router to the guest (fallback-safe)

**Files:**

- Modify: `projects/monolith/goosecracker/dispatch.py` (accept an optional `plan`)
- Modify: `projects/monolith/goosecracker/runner.py` (payload construction ~613-624)
- Modify: `projects/monolith/goosecracker/tests/sessions_test.py` or a new `dispatch_test.py`
- Modify: `projects/monolith/chat/bot.py:1086` area (pass the `PlanVerdict` through)

**Step 1: Failing tests** — when a `Plan` is present, the fc-invoke payload sets `recipe == "/injected-context/router.yaml"` and BOTH `injectedContext["router.yaml"] == render_router(plan)` AND `injectedContext["plan.md"] == render_plan_file(plan)`; when absent (fallback), `recipe == "agent"` and neither key is injected. Per-step context files (if any) are injected under basename keys only (no traversal).

**Step 2: Implement** — thread an optional `plan: Plan | None` from `bot.py`'s `compile` result into `dispatch.submit` → `runner`. In `_run_one_turn`, if `plan` is set, render both the router and the plan file, add them to `injected_context` under `"router.yaml"` and `"plan.md"` respectively (the guest sees them at `/injected-context/router.yaml` and `/injected-context/plan.md`), and set `recipe="/injected-context/router.yaml"`; else keep today's `recipe="agent"`. Preserve all existing injectedContext (ADR 040 per-turn context) — the router and plan file are two more keys. **Step 3: Commit.**

```bash
git commit -m "feat(goosecracker): inject runtime router via injectedContext, fallback to baked agent"
```

---

## Task 7: Capped replan escape hatch

**Files:**

- Modify: `projects/monolith/goosecracker/runner.py` (`run_and_deliver` loop ~680-769)
- Create: `projects/monolith/goosecracker/replan.py` (parse the structured signal)
- Modify: `projects/monolith/goosecracker/tests/` (new `replan_test.py`)

**Step 1: Failing tests** — `parse_replan(result_text) -> ReplanRequest | None` extracts the optional `replan` object from goose's `recipe__final_output` JSON (returned verbatim in `AgentResult.Result`); returns `None` when absent or unparseable. The `run_and_deliver` loop: on a replan request with `count < 3`, re-invokes `orchestrator.compile` with accumulated context (prior plan + `what_i_learned` + `suggested_focus`, replan timeout **120s**), re-renders, re-invokes goose; at `count == 3` it stops and finalizes with the last result (no further replan); a malformed/absent signal ends the session normally (today's behavior).

**Step 2: Implement** — add a host-side `replan_count` to the `run_and_deliver` loop (distinct from the Discord-queue drain loop; a replan re-runs the same turn with a new router, a queue-drain advances to the next message). Enforce the cap deterministically host-side (never trust a recipe-side counter). On orchestrator unavailability during replan, finalize with the current result (fail-open). Instrument each replan (count, reason, latency) to telemetry. **Step 3: Commit.**

```bash
git commit -m "feat(goosecracker): capped (3) DeepSeek replan loop for goose escape hatch"
```

---

## Task 8: Timeouts, ack-first, telemetry

**Files:**

- Modify: `projects/monolith/chat/orchestrator_client.py` (already takes `timeout_s` from Task 3)
- Modify: `projects/monolith/chat/orchestrator.py` (`_read_config`: `ORCHESTRATOR_TIMEOUT_S` default 60; add `ORCHESTRATOR_REPLAN_TIMEOUT_S` default 120)
- Modify: `projects/monolith/chat/bot.py` (post the ⏳ ack BEFORE calling `compile`)
- Modify: `projects/monolith/chart/values.yaml` + `chart/templates/deployment.yaml` (surface both timeouts as env)
- Modify: relevant `*_test.py` asserting the new defaults

**Step 1:** `grep` the test tree for the old `10`/`10.0` timeout default and update assertions in the same commit (CLAUDE.md: bumping config values that tests assert on).
**Step 2:** Default initial compile 60s; replan 120s; both env-configurable. Ack-first: move the queue ⏳ reaction to before `compile` so plan latency is hidden.
**Step 3:** Telemetry: plan-call latency + which timeout applied. **Step 4: Commit.**

```bash
git commit -m "feat(chat): 60s plan / 120s replan timeouts, ack-first, latency telemetry"
```

---

## Task 9: Chart version bump + push + CI

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (bump version)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision` in sync — CLAUDE.md key pattern)

**Step 1:** Bump chart version once for the whole PR; sync `targetRevision`.
**Step 2:** `format` (updates BUILD + home-cluster root). **Step 3:** Push branch, open PR, watch CI.

```bash
git commit -m "chore(monolith): bump chart version for runtime-recipe orchestrator"
git push -u origin feat/deepseek-runtime-recipes
gh pr create --fill
gh pr checks <n> --watch
```

**Step 4:** Diagnose any red via `mcp__buildbuddy__get_invocation` (commitSha selector) → `get_target` → `get_log`; quote the assertion before hypothesizing (CLAUDE.md). **Step 5:** One end-of-PR comprehensive code review (Opus reviewer) against the full diff before merge (CLAUDE.md review cadence). Merge with `gh pr merge --rebase`.

---

## Out of scope (deliberately deferred)

- Dedicated `recipeFiles` payload field / any guest Go change — `injectedContext` suffices; revisit only if we outgrow it.
- Per-tier recipe variants, fileless recipes in Postgres/S3 — keeps recipes in-repo for the `/improve-recipes` loop.
- ADR: this amends ADR 040 (injection channel) and ADR 036 (orchestrator). Write the ADR as a fast-follow (rationale-only, per CLAUDE.md ADR convention); it does not block the PR.
