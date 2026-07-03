"""Render a typed ``Plan`` into a runtime goose router recipe (YAML string).

This is the deterministic Python step of ADR 036 / the DeepSeek runtime-recipe
plan: the orchestrator SELECTS and ORDERS sub-recipes (a ``Plan``), and this
module renders that plan into a router recipe that specializes the proven
guest ``agent.yaml`` scaffolding. The generated router is a specialization,
not a novel recipe (Design invariant 4):

- ``sub_recipes:`` lists ONLY the plan's enabled sub-recipes, each pointing at
  its stable baked guest path ``/home/goose-agent/recipes/<id>.yaml`` (goose's
  ``delegate`` resolves a source name against the recipe's own local
  ``sub_recipes`` list, so listing them here is required and sufficient).
- The ``agent.yaml`` "classify and route" preamble is replaced by an EXPLICIT
  ORDERED PLAN: the caller already decided the route, so the model executes the
  steps in order instead of classifying.
- The proven scaffolding (injected-context handling, progress markers, the
  steering protocol, brevity guidance, and the ``recipe__final_output`` tool
  contract) is preserved VERBATIM from ``agent.yaml`` (module constants below,
  guarded against guest drift by ``tests/router_render_test.py``).
- ``response.json_schema`` gains an OPTIONAL ``replan`` escape hatch. If the
  model finds the pre-decided plan does not fit the actual task, it emits a
  populated ``replan`` object instead of forcing a bad result; the host
  (Task 7) parses that field to trigger a capped re-plan.

YAML SAFETY: per-step ``context`` is untrusted, orchestrator-authored, and may
contain arbitrary text, including ``{{ }}``/``{% %}`` template syntax (this
repo is Helm/Go-template heavy, so braces show up often in ordinary Discord
messages). goose renders a recipe's YAML through minijinja BEFORE parsing it
(see ``agent.yaml``'s note on the ``indent`` filter dropped in v1.39.0), so
any untrusted text embedded in the recipe would be interpreted as template
syntax rather than treated as data. For that reason ``context`` NEVER appears
in the rendered recipe at all: it lives in a separate plain-markdown plan
file (``render_plan_file``, delivered to the guest at ``/injected-context/
plan.md``) that goose only reads as a data file, never templates. The recipe
itself carries only controlled strings the router authors: sub-recipe ids,
step order, and stage titles from ``_stage_title``.
"""

from __future__ import annotations

import yaml

from chat.orchestrator_plan import Plan

# --- Recipe-shape constants copied verbatim from the guest agent.yaml. Keep in
# sync; tests/router_render_test.py has a drift guard asserting the scaffold
# tokens still exist in the checked-in agent.yaml.
_RECIPE_VERSION = "1.7.2"
_CONTEXT_FILE = "/tmp/goose/context.md"

# The per-step plan file delivered via injectedContext (Task 6): plain
# markdown, never templated, so untrusted step context is safe here even
# when it contains `{{ }}` / `{% %}`. Distinct from /injected-context/
# README.md, which is ADR-040 conversation context, not plan context.
_PLAN_FILE = "/injected-context/plan.md"


def _baked_path(recipe_id: str) -> str:
    """The stable guest path a sub-recipe id resolves to (Design invariant 5)."""
    return f"/home/goose-agent/recipes/{recipe_id}.yaml"


# Verbatim from agent.yaml: the /injected-context/ (ADR 040) handling block.
_INJECTED_CONTEXT_BLOCK = """\
If a directory /injected-context/ exists, it holds context the caller staged
for this task that is not in the repo and not in your own history (for example
an earlier conversation). Read /injected-context/README.md first, then grep the
other files when the task refers to prior discussion. Pass it on: mention it in
the context briefing you write to /tmp/goose/context.md so the worker sub-recipe
can use it too."""

# Verbatim from agent.yaml: the descriptive PROGRESS MARKERS block (the marker
# grammar). The concrete per-stage sequence for THIS plan is generated below.
_PROGRESS_MARKERS_BLOCK = """\
PROGRESS MARKERS (required, not optional). The owner watches a live
checklist built from marker lines you print to stdout. Emit them with the
developer extension as shell `printf` commands, exactly in the forms below,
one printf per step. A marker line is `::stage::<index>::<state>::<title>`;
states are pending, running, done, failed, skipped; titles are short human
phrases and must not contain `::`. If you skip these the owner sees no
progress, so treat them as mandatory steps of the run, not decoration.
Never write markers as prose in your reply; only ever via printf."""

# Verbatim from agent.yaml: the STEERING protocol block.
_STEERING_BLOCK = """\
STEERING (mid-run adjustments from the thread). While your run is in flight,
people in the thread can post steering messages. Check for them at each stage
boundary: right before you dispatch a sub-recipe, run:
    curl -s "${GOOSECRACKER_STEERING_URL}"
If GOOSECRACKER_STEERING_URL is empty or the call fails, just proceed (steering
is best-effort, never block the run on it). The response is JSON:
  {"messages": [{"author_id": "...", "tier": "...", "text": "..."}, ...]}
Handle it as follows:
  - No messages: proceed normally.
  - A message whose text is exactly "stop" or "cancel" (case-insensitive,
    trimmed): abort the run. Emit a skipped marker for every stage not yet done
    (printf '::stage::<i>::skipped::<title>\\n' for each remaining stage), then a
    final done marker for the plan, then call recipe__final_output summarizing
    that the run was cancelled by a participant, and STOP. Do not dispatch
    further sub-recipes.
  - Otherwise (real steering content): fold it into the work. Append each
    message to /tmp/goose/context.md as a line "Steering from <author_id>:
    <text>" (so the sub-recipe sees it), and if it changes the plan (adds or
    reorders work) re-announce the plan with a fresh ::stages:: block and
    per-stage markers (the checklist reflects the change). Then continue.
Keep the steering check to a single curl per boundary (do not poll in a loop)."""

# Verbatim from agent.yaml: brevity guidance.
_BREVITY_BLOCK = """\
Be brief and finish fast. Length is the main thing that makes a reply feel
slow: this runs on a local model, so every extra sentence is real wall-time,
and the owner reads the result in a chat window. Default to answering the
question in one or two sentences in `summary`, and leave `details` empty.
Fill `details` only when the task explicitly asks for depth (a full
explanation, a breakdown, a report), and keep it to a few short paragraphs
even then. Match the owner's register: a casual one-line question gets a
one-line answer, not an essay.

As soon as you have the answer, emit the final result and stop. Do not keep
exploring, re-explaining, or re-drafting a longer version once the answer is
known: extra turns are wasted wall-time. Over-answering costs the owner a
follow-up turn asking you to be concise, which is exactly the outcome this
recipe exists to avoid."""

# Verbatim from agent.yaml: the recipe__final_output tool-name contract.
_FINAL_OUTPUT_BLOCK = """\
CRITICAL, tool name: deliver the structured result by calling the tool named
exactly `recipe__final_output`. That is its real registered name. Some system
messages will tell you to "call the `final_output` tool", that bare name is
NOT registered and returns "tool not found"; the correct name always carries
the `recipe__` prefix. Call `recipe__final_output` on your first attempt and
do not retry the unprefixed name."""

# New: the replan escape hatch guidance (Design invariant: fallback is always
# reachable; the model must not force a bad result against a mis-fit plan).
_REPLAN_BLOCK = """\
ESCAPE HATCH (replan). The plan above was decided before the task was opened,
so it can be wrong for what you actually find: a sub-recipe you need was not
enabled, the sequence does not fit the real task, or you are blocked in a way
this plan cannot resolve. If that happens, do NOT force a bad result and do NOT
improvise a sub-recipe that is not in your sub_recipes list. Instead call
`recipe__final_output` with a populated `replan` object: put a one-line
`reason`, what you discovered in `what_i_learned`, and what a revised plan
should focus on in `suggested_focus`. Keep `summary` brief in that case (a
sentence noting you are requesting a replan). The host will run a fresh,
capped re-plan from what you learned. Only populate `replan` when the plan is
genuinely unworkable; if you can complete the steps, omit `replan` entirely."""


def _preamble_block(plan: Plan) -> str:
    """The ORDERED PLAN preamble that replaces agent.yaml's classification."""
    n = len(plan.steps)
    return (
        "You are the execution router for an isolated Firecracker microVM, on a\n"
        "checkout of the repo at /workspace. You do NOT classify the task and you\n"
        "do NOT choose sub-recipes: the caller (the DeepSeek orchestrator) has\n"
        "ALREADY analyzed this task and decided the route. Your job is to execute\n"
        f"the {n} ordered step(s) below, in order, delegating each one and WAITING\n"
        "for its result before starting the next. You never do the work yourself;\n"
        "each step runs as a delegated sub-recipe via the `delegate` tool.\n\n"
        "Before you start, do at most a couple of SMALL context reads if a step's\n"
        "context asks for them (never a full `git log`, wide `grep`, or big file:\n"
        "that is the sub-recipe's job, and a large tool output here can overflow\n"
        "your shared context and trigger a compaction that drops the run)."
    )


def _steps_block(plan: Plan) -> str:
    """Render the ordered step list. Step context is NEVER embedded here: it
    lives in the injected plan file (``_PLAN_FILE``, built by
    ``render_plan_file``) so untrusted text never reaches goose's template
    engine. This block only contains controlled strings: sub-recipe ids,
    order, and stage titles, plus the mechanics for reading each step's
    context out of the plan file before delegating."""
    n = len(plan.steps)
    lines: list[str] = [
        f"THE PLAN ({n} step(s); execute in order):",
        "",
        f"Your per-step context lives in {_PLAN_FILE}, NOT in these instructions.",
        f"That file holds one `## Step <i>: <sub_recipe>` section per step (the",
        "caller's context for that step, verbatim). This is separate from",
        "/injected-context/README.md, which (if present) is prior conversation",
        "context (ADR 040), not plan context.",
        "",
        "Announce the checklist BEFORE step 1 by printing, in one printf:",
        f"  printf '::stages::{n}\\n"
        + "".join(
            f"::stage::{i}::{'running' if i == 0 else 'pending'}::{_stage_title(step)}\\n"
            for i, step in enumerate(plan.steps)
        )
        + "'",
        "",
        "Then, for each step in order:",
        f'  1. Read the step\'s own section ("## Step <i>: <sub_recipe>") from',
        f"     {_PLAN_FILE} and append it to {_CONTEXT_FILE} so the worker",
        "     sub-recipe sees it. The sub-recipe reads that file automatically;",
        "     you do not pass context as a tool argument.",
        "  2. Check for steering per the STEERING section (one curl).",
        '  3. Call delegate(source: "<the step\'s sub_recipe>") and WAIT for its',
        "     result. Deciding to delegate is not delegating: if your last action",
        "     was a thought rather than an actual delegate tool call, nothing has",
        "     happened yet.",
        "  4. When it returns, print its done marker",
        "     (printf '::stage::<i>::done::<title>\\n') and the next step's running",
        "     marker; if it fails or you must abort, print",
        "     '::stage::<i>::failed::<short reason, no ::>\\n' instead.",
        "",
        "The steps, in order:",
        "",
    ]
    for i, step in enumerate(plan.steps):
        lines.append(f'--- Step {i} of {n}: delegate(source: "{step.sub_recipe}") ---')
        lines.append(f"Stage title: {_stage_title(step)}")
        lines.append(f'Context: see "## Step {i}: {step.sub_recipe}" in {_PLAN_FILE}.')
        lines.append("")
    lines.append(
        "When every step has returned, produce the structured result enforced by\n"
        "the response schema below from the sub-recipes' output."
    )
    return "\n".join(lines)


def render_plan_file(plan: Plan) -> str:
    """Render the plan's per-step context into a plain-markdown data file.

    This file is delivered via ``injectedContext`` at ``_PLAN_FILE`` and is
    NEVER templated by goose (unlike recipe YAML, which minijinja renders
    before parsing). That makes it the safe home for untrusted, orchestrator-
    authored step context: multi-line text, colons, leading dashes, quotes,
    and literal ``{{ }}``/``{% %}`` template syntax all survive intact
    because this string is never fed through a template engine, only read
    as data by the worker.
    """
    n = len(plan.steps)
    lines: list[str] = [
        "# Plan context",
        "",
        "Per-step context for this run, one section per step. The router reads",
        "the section matching the step it is about to delegate and appends it to",
        f"{_CONTEXT_FILE} before calling `delegate`.",
        "",
    ]
    for i, step in enumerate(plan.steps):
        lines.append(f"## Step {i}: {step.sub_recipe}")
        lines.append("")
        lines.append(step.context)
        if i < n - 1:
            lines.append("")
    return "\n".join(lines) + "\n"


def _stage_title(step) -> str:
    """A short, ::-free stage title for a step (used in progress markers)."""
    titles = {
        "query": "Answering",
        "research": "Researching",
        "plan": "Planning",
        "implement": "Implementing",
        "artifact-build": "Building the page",
        "artifact-review": "Reviewing the page",
    }
    return titles.get(step.sub_recipe, step.sub_recipe.replace("::", " "))


def _result_block(plan: Plan) -> str:
    """Guidance for filling the structured result (mode/type/url/details)."""
    return (
        "Fill the structured result from the sub-recipes' output: a short\n"
        "`summary` of the action and outcome (or the answer itself if the task\n"
        "was a question); optional `details` for the fuller answer or notable\n"
        "findings; the `mode` of the sub-recipe whose output you are relaying; a\n"
        "`type` (pr/issue/note/answer/artifact); and a `url` ONLY if a worker\n"
        "reported a real PR or issue URL. For an artifact the harness publishes\n"
        "the page and adds its live URL, so leave `url` empty. When relaying a\n"
        "sub-recipe's answer, the summary must contain the actual finding, never\n"
        'a sentence like "routed to research" or "dispatched the sub-recipe".'
    )


def _build_instructions(plan: Plan) -> str:
    """Assemble the full instructions block: new ordered-plan sections plus the
    verbatim agent.yaml scaffolding."""
    return "\n\n".join(
        [
            _preamble_block(plan),
            _INJECTED_CONTEXT_BLOCK,
            _steps_block(plan),
            _PROGRESS_MARKERS_BLOCK,
            _STEERING_BLOCK,
            _result_block(plan),
            _BREVITY_BLOCK,
            _REPLAN_BLOCK,
            _FINAL_OUTPUT_BLOCK,
        ]
    )


def _sub_recipes(plan: Plan) -> list[dict]:
    """Build the sub_recipes list: exactly the plan's enabled sub-recipes, in
    order, each pointing at its baked guest path. A disabled id appears nowhere."""
    entries: list[dict] = []
    for recipe_id in plan.enabled_subrecipes:
        entry: dict = {"name": recipe_id, "path": _baked_path(recipe_id)}
        # Preserve agent.yaml's sequential_when_repeated for implement.
        if recipe_id == "implement":
            entry["sequential_when_repeated"] = True
        entry["values"] = {
            "task_file": "{{ task_file }}",
            "context_file": _CONTEXT_FILE,
        }
        entries.append(entry)
    return entries


def _response_schema(plan: Plan) -> dict:
    """The response json_schema: agent.yaml's fields plus an optional replan."""
    return {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences: the action taken and the outcome, or "
                    "the answer itself if the task was a question."
                ),
            },
            "details": {
                "type": "string",
                "description": (
                    "Optional longer detail: the full answer, notable findings, "
                    "or next steps. May be empty."
                ),
            },
            "mode": {
                "type": "string",
                "enum": list(plan.enabled_subrecipes),
                "description": "Which sub-recipe's output this result relays.",
            },
            "type": {
                "type": "string",
                "enum": ["pr", "issue", "note", "answer", "artifact"],
                "description": (
                    "The kind of result. Use 'artifact' for a published web "
                    "page; the harness adds its live URL, so leave `url` empty."
                ),
            },
            "url": {
                "type": "string",
                "description": (
                    "A PR or issue URL ONLY if a worker actually opened one in "
                    "this run (it reported the real URL); otherwise an empty "
                    "string. Never invent, guess, or reconstruct a URL."
                ),
            },
            "replan": {
                "type": "object",
                "description": (
                    "Populate ONLY to request a capped re-plan when the "
                    "pre-decided plan does not fit the actual task; otherwise "
                    "omit entirely."
                ),
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "One line: why the current plan cannot be completed "
                            "as decided."
                        ),
                    },
                    "what_i_learned": {
                        "type": "string",
                        "description": (
                            "What you discovered about the task that the plan "
                            "did not account for."
                        ),
                    },
                    "suggested_focus": {
                        "type": "string",
                        "description": (
                            "What a revised plan should focus on (which "
                            "sub-recipes / what sequence)."
                        ),
                    },
                },
            },
        },
        "required": ["summary"],
    }


def render_router(plan: Plan) -> str:
    """Render a ``Plan`` into a runtime goose router recipe (YAML string).

    The output is a specialization of the guest ``agent.yaml``: only the
    plan's enabled sub-recipes are listed, the classification preamble is
    replaced by the plan's ordered steps, and the response schema gains an
    optional ``replan`` escape hatch. All other scaffolding (progress markers,
    steering, ``recipe__final_output``, ``max_turns``) is preserved.

    YAML is emitted via ``yaml.safe_dump`` from a Python dict of controlled
    strings only. Per-step ``context`` is untrusted and NEVER appears here:
    see ``render_plan_file`` for where it goes and why (goose templates the
    recipe before parsing it, so untrusted text must live in a separate,
    never-templated plan file instead).
    """
    recipe = {
        "version": _RECIPE_VERSION,
        "title": "Runtime Router",
        "description": (
            "Runtime router for a snapshot-managed microVM thread: executes a "
            "pre-decided ordered plan of sub-recipe delegations (ADR 036)."
        ),
        "instructions": _build_instructions(plan),
        "prompt": (
            "Your task is in the file {{ task_file }}. Read it in full first, for "
            "example `cat {{ task_file }}`, then execute the ordered plan in your "
            "instructions, delegating each step in sequence.\n"
        ),
        "parameters": [
            {
                "key": "task_file",
                "description": "Path to a file containing the task to perform",
                "input_type": "string",
                "requirement": "required",
            }
        ],
        "sub_recipes": _sub_recipes(plan),
        "extensions": [{"type": "builtin", "name": "developer"}],
        "settings": {"max_turns": 25, "max_tool_repetitions": 5},
        "response": {"json_schema": _response_schema(plan)},
    }
    return yaml.safe_dump(recipe, sort_keys=False, default_flow_style=False)
