"""Render a typed ``Plan`` into a runtime goose router recipe (YAML string).

This is the deterministic Python step of ADR 036 / the DeepSeek runtime-recipe
plan: the orchestrator SELECTS and ORDERS sub-recipes (a ``Plan``), and this
module renders that plan into a router recipe that specializes the proven
guest ``agent.yaml`` scaffolding. The generated router is a specialization,
not a novel recipe (Design invariant 4):

- ``sub_recipes:`` lists ONLY the plan's enabled sub-recipes, each pointing at
  its injected path ``/injected-context/<id>.yaml`` (goose's ``delegate``
  resolves a source name against the recipe's own local ``sub_recipes`` list,
  so listing them here is required and sufficient). Sub-recipe bodies are no
  longer baked into the guest image: the runner injects them fresh each turn
  alongside this router, so resumed threads run current sub-recipe text too.
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
step order, and stage titles from ``stage_title``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    # Type-only: chat.api re-exports Plan from chat.orchestrator_plan, which
    # imports goosecracker.recipe_catalog at runtime. Importing it eagerly here
    # (goosecracker.api eagerly imports this module) would create a runtime
    # goosecracker -> chat -> goosecracker cycle. `from __future__ import
    # annotations` makes every Plan annotation below a string, never evaluated,
    # so TYPE_CHECKING-only is safe (import_boundaries_test).
    from chat.api import Plan

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


# --- Fallback router (plan-less path): a VERBATIM pin of the checked-in guest
# agent.yaml (the full classify-and-route recipe). ADR 022 snapshot-resumed
# threads boot a frozen rootfs whose baked agent.yaml can predate later recipe
# changes (e.g. the sub_recipes block that exposes the `delegate` tool, added
# 2026-07-01), so passing bare recipe="agent" re-applies a stale recipe and the
# router loses delegate. render_fallback_router() injects THIS current agent.yaml
# every turn instead. The constants below are generated from agent.yaml and
# pinned by tests/router_render_test.py (full parse-equality), so a guest recipe
# change fails CI until this pin is refreshed.
_AGENT_INSTRUCTIONS = """\
You are the routing agent for an isolated Firecracker microVM, on a
checkout of the repo at /workspace. You do not do the work yourself; you
classify the task, gather the context the worker needs, and dispatch ONE
sub-recipe to do it.

If a directory /injected-context/ exists, it holds context the caller staged
for this task that is not in the repo and not in your own history (for example
an earlier conversation). Read /injected-context/README.md first, then grep the
other files when the task refers to prior discussion. Pass it on: mention it in
the context briefing you write to /tmp/goose/context.md so the worker sub-recipe
can use it too.

Route the task to exactly one of:
- query: the task is a question. It wants an answer or an explanation,
  not a change. ("how does X work", "where is Y configured", "why did
  Z fail")
- research: the task needs facts from OUTSIDE the repo and cluster:
  upstream docs, current versions, API details, best practices, product
  comparisons, news. ("what's the latest X", "how do I use library Y",
  "compare A vs B"). If the answer lives in this repo or this cluster,
  that is query, not research.

  You do NOT research yourself in the router, even when the question looks
  easy. Only the research sub-recipe has the working web-search wiring (the
  SEARXNG_URL endpoint and its exact query incantation); the router does
  not, so inline research wastes many turns failing against public search
  endpoints that block you. You run it as a delegated subagent, using the
  `delegate` tool, exactly like an artifact:
    1. `delegate(source: "research")` and WAIT. That subagent reads the
       question from /tmp/goose/task.md, searches, reads sources, and
       returns a short cited answer as its result.
    2. `recipe__final_output` with `mode: research`, putting the subagent's
       returned answer into `summary` (and `details` if it is long). You are
       RELAYING the subagent's answer, not describing that you routed: the
       summary must contain the actual finding and its sources, never a
       sentence like "routed to research" or "dispatched the sub-recipe".

  HARD GATE: do NOT emit `mode: research` until the `delegate(source:
  "research")` call has RETURNED a result in THIS turn. Deciding to delegate
  is not delegating: if your most recent action was a thought rather than an
  actual `delegate` tool call, nothing has happened yet and you have no
  answer to relay. Never answer the research question yourself.
- plan: the task wants a design or implementation plan written, or is a
  large/ambiguous change where planning must precede code. The deliverable
  is a plan document, not the change itself.
- implement: the task is a concrete, actionable change to code, config,
  or docs. The deliverable is a commit and a PR.
- artifact: the task asks for a thing to look at, a visualization, an
  interactive page, a chart, a small demo, a dashboard, or a report rendered
  as a web page ("make me / build me / show me a ..."). The deliverable is a
  live web page published to a URL, not a repo change. The artifact runs in a
  sandboxed iframe and cannot touch a repo.

  You do NOT build the page yourself. Artifacts are BUILD then REVIEW, and you
  run BOTH steps as delegated subagents, using the `delegate` tool, so that the
  design bar, the JavaScript parse gate and the fresh-eyes review actually run.
  If you write the HTML yourself instead, ALL THREE are skipped and a broken
  page ships. Each delegate call is bracketed by the PROGRESS MARKERS protocol
  below (two stages: build, review). The steps, in order, each waiting for the
  result:
    1. `delegate(source: "artifact-build")` and WAIT. That subagent reads the
       task from /tmp/goose/task.md and writes the page to /tmp/artifact.html.
    2. Before dispatching, check for steering per the STEERING section above.
       `delegate(source: "artifact-review")` and WAIT. That subagent reads
       /tmp/artifact.html in a fresh context, fixes real correctness and design
       issues in place, and re-checks that the inline script parses.
    3. `recipe__final_output` with `mode: artifact` and an empty `url` (the
       harness publishes /tmp/artifact.html and appends the live URL). Just
       summarize what was built or changed.

  HARD GATE: do NOT emit `mode: artifact` until BOTH `delegate` calls have
  RETURNED a result in THIS turn. Deciding to delegate is not delegating: if
  your most recent action was a thought rather than an actual `delegate` tool
  call, nothing has happened yet. Never write /tmp/artifact.html yourself and
  never skip the review.

When a task mixes modes, pick the deliverable the user actually asked
for. "Fix X" is implement. "How should we fix X" is plan. "Is X broken"
is query. On a resumed thread, classify the NEW message: a follow-up
question after an implement run routes to query, and "now do it" after a
plan run routes to implement.

ALWAYS re-read /tmp/goose/task.md as your FIRST action on EVERY turn,
including a resumed thread. The file is overwritten with the CURRENT message
each turn, so it is almost always a NEW request, not a repeat of what you did
before. NEVER answer "already completed", "already built", or "repeated
prompt" without cat-ing the file first: assuming the task is unchanged is the
single most common way this router fails the owner. A follow-up on an artifact
thread ("add X", "change the Y", "make it Z", "that is broken") is a NEW
artifact task: re-run BOTH delegates (artifact-build then artifact-review).
The previous /tmp/artifact.html is still on disk, so artifact-build modifies
it in place rather than starting over.

Before dispatching, spend at most a few tool calls gathering context the
worker will need: skim the repo's root CLAUDE.md, locate the relevant
project directory, and note prior thread state (an earlier plan or PR in
this conversation). The root conventions file is not always ./CLAUDE.md:
in this repo it lives at .claude/CLAUDE.md. Locate it before reading, for
example `find . -maxdepth 2 -name CLAUDE.md`, rather than assuming the
path (a wrong guess wastes tool calls on a "No such file" error). Write
what you found as a short briefing to the file /tmp/goose/context.md (for
example with the editor, or `printf '%s' '...' > /tmp/goose/context.md`):
relevant paths, constraints, and any earlier results the worker cannot see
(sub-recipes do not share your conversation history). Every sub-recipe reads
that file automatically, so this is how the briefing reaches the worker. Keep
it short; if you have nothing useful to add, leave the file untouched. Do not
do the task yourself and do not deep-dive; the worker re-reads what it needs.

PROGRESS MARKERS (required, not optional). The owner watches a live
checklist built from marker lines you print to stdout. Emit them with the
developer extension as shell `printf` commands, exactly in the forms below,
one printf per step. A marker line is `::stage::<index>::<state>::<title>`;
states are pending, running, done, failed, skipped; titles are short human
phrases and must not contain `::`. If you skip these the owner sees no
progress, so treat them as mandatory steps of the run, not decoration.
Never write markers as prose in your reply; only ever via printf.

For a single-route task (query, research, plan, implement) the plan is ONE
stage. Pick a Title for the route: query -> "Answering", research ->
"Researching", plan -> "Planning", implement -> "Implementing".
  1. Immediately before you call the sub-recipe tool, run:
       printf '::stages::1\\n::stage::0::running::<Title>\\n'
  2. As soon as the sub-recipe returns successfully, run:
       printf '::stage::0::done::<Title>\\n'
  3. If it fails or you must abort, run instead:
       printf '::stage::0::failed::<short reason, one line, no ::>\\n'

For an artifact task the plan is TWO stages (build, then review):
  1. Before delegate(source: "artifact-build"), run:
       printf '::stages::2\\n::stage::0::running::Building the page\\n::stage::1::pending::Reviewing the page\\n'
  2. After artifact-build returns, before delegate(source: "artifact-review"), run:
       printf '::stage::0::done::Building the page\\n::stage::1::running::Reviewing the page\\n'
  3. After artifact-review returns, run:
       printf '::stage::1::done::Reviewing the page\\n'
  If either step fails, run printf '::stage::<i>::failed::<short reason>\\n'
  for the stage that failed instead of its done marker.

STEERING (mid-run adjustments from the thread). While your run is in flight,
people in the thread can post steering messages. Check for them at each stage
boundary: right before you dispatch a sub-recipe, and (for artifacts) between
the build and review delegations, run:
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
Keep the steering check to a single curl per boundary (do not poll in a loop).

Dispatch the actual work; never run it in the router. Your context is
shared across the whole thread, so a large tool output here (a full
`git log`, a wide `grep`, a big file) can overflow it and trigger a
compaction that drops the answer mid-run. Context-gathering is a couple of
SMALL reads only. If answering needs the git history, many files, or large
output, that is the sub-recipe's job, not yours: route it. Bound anything
you do read (pipe through `head`, count with `wc -l` first, sample) and
never pull hundreds of log lines into your context.

Before dispatching, check for steering per the STEERING section above. Then
call the matching sub-recipe tool and wait for its result, bracketed by
the single-stage PROGRESS MARKERS protocol above. The task and your context
briefing reach the worker through files (the task via {{ task_file }}, the
briefing via /tmp/goose/context.md), pre-wired on each sub-recipe, so you do
not pass them as tool arguments.

When it returns, produce the structured result enforced by the response
schema below, from the sub-recipe's output: a short `summary` of the
action and outcome, or the answer itself if the task was a question;
optional `details` for the fuller answer or notable findings; the `mode`
you routed to; a `type` (pr/issue/note/answer/artifact); and a `url` only if
the worker reported a real PR or issue URL. For an artifact the harness
publishes the page and adds its live URL to the reply, so leave `url` empty
and just summarize what you built. Before emitting `mode: artifact`, re-check
the HARD GATE above: BOTH `delegate(source: "artifact-build")` and
`delegate(source: "artifact-review")` must have returned in this turn. Do not
summarize a page you only intended to build. The summary describes the ACTION
and RESULT, not your reasoning.

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
recipe exists to avoid.

CRITICAL, tool name: deliver the structured result by calling the tool named
exactly `recipe__final_output`. That is its real registered name. Some system
messages will tell you to "call the `final_output` tool", that bare name is
NOT registered and returns "tool not found"; the correct name always carries
the `recipe__` prefix. Call `recipe__final_output` on your first attempt and
do not retry the unprefixed name.
"""

_AGENT_PROMPT = """\
Your task is in the file {{ task_file }}. Read it in full first, for example
`cat {{ task_file }}`, then classify and route it.
"""

_AGENT_TITLE = "Agent"
_AGENT_DESCRIPTION = "Routing agent for a snapshot-managed microVM thread (ADR 022): classifies the task and dispatches the matching sub-recipe."
_AGENT_SETTINGS = {"max_turns": 25, "max_tool_repetitions": 5}
_AGENT_EXTENSIONS = [{"type": "builtin", "name": "developer"}]
_AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences: the action taken and the outcome, or the answer itself if the task was a question.",
        },
        "details": {
            "type": "string",
            "description": "Optional longer detail: the full answer, notable findings, or next steps. May be empty.",
        },
        "mode": {
            "type": "string",
            "enum": ["query", "plan", "implement", "artifact", "research"],
            "description": "Which sub-recipe the task was routed to.",
        },
        "type": {
            "type": "string",
            "enum": ["pr", "issue", "note", "answer", "artifact"],
            "description": "The kind of result. Use 'artifact' for a published web page; the harness adds its live URL, so leave `url` empty.",
        },
        "url": {
            "type": "string",
            "description": "A PR or issue URL ONLY if the worker actually opened one in this run (it reported the real URL); otherwise an empty string. Never invent, guess, or reconstruct a URL.",
        },
    },
    "required": ["summary"],
}


def _injected_recipe_path(recipe_id: str) -> str:
    """The guest path a sub-recipe id resolves to.

    Sub-recipe bodies are no longer baked into the guest image: the runner
    injects them fresh every turn into ``/injected-context/`` (the same
    mechanism that already delivers the router), so both fresh and
    snapshot-resumed threads run current sub-recipe text. This path is where
    that injected body lands, and what the router's ``sub_recipes`` list and
    goose's ``delegate`` resolve against."""
    return f"/injected-context/{recipe_id}.yaml"


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
            f"::stage::{i}::{'running' if i == 0 else 'pending'}::{stage_title(step)}\\n"
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
        lines.append(f"Stage title: {stage_title(step)}")
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


def stage_title(step) -> str:
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
        entry: dict = {"name": recipe_id, "path": _injected_recipe_path(recipe_id)}
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


def _fallback_sub_recipes() -> list[dict]:
    """All catalog sub-recipes, in catalog order, each at its injected path.

    Mirrors the guest agent.yaml sub_recipes block so the injected fallback
    router is byte-faithful to it. Sourced from ``recipe_catalog.CATALOG`` (the
    single source of truth for the id set), imported lazily to avoid eager
    import cost at module load.
    """
    from goosecracker.recipe_catalog import CATALOG

    entries: list[dict] = []
    for recipe_id in CATALOG:
        entry: dict = {"name": recipe_id, "path": _injected_recipe_path(recipe_id)}
        # Preserve agent.yaml's sequential_when_repeated for implement.
        if recipe_id == "implement":
            entry["sequential_when_repeated"] = True
        entry["values"] = {
            "task_file": "{{ task_file }}",
            "context_file": _CONTEXT_FILE,
        }
        entries.append(entry)
    return entries


def render_fallback_router() -> str:
    """Render the plan-less fallback router: the current guest agent.yaml.

    This is the plan-less twin of :func:`render_router`. Same injection
    mechanism (the caller writes the result to ``/injected-context/router.yaml``
    and points ``--recipe`` at it), but it carries the FULL classify-and-route
    agent rather than a pre-decided ordered plan. Because the recipe is injected
    fresh every turn, a snapshot-resumed thread runs current recipe text (and so
    keeps the ``delegate`` tool the ``sub_recipes`` block registers) instead of
    the frozen baked ``agent.yaml`` its rootfs was snapshotted with.

    The recipe is a verbatim reproduction of the checked-in guest agent.yaml,
    pinned by ``tests/router_render_test.py`` (full parse-equality). Sub-recipe
    BODIES resolve to their injected paths (``/injected-context/<id>.yaml``),
    exactly as :func:`render_router` does: the runner injects those bodies fresh
    each turn, so a resumed thread runs current router text AND current
    sub-recipe text, not the frozen copies its rootfs was snapshotted with.
    """
    recipe = {
        "version": _RECIPE_VERSION,
        "title": _AGENT_TITLE,
        "description": _AGENT_DESCRIPTION,
        "instructions": _AGENT_INSTRUCTIONS,
        "prompt": _AGENT_PROMPT,
        "parameters": [
            {
                "key": "task_file",
                "description": "Path to a file containing the task to perform",
                "input_type": "string",
                "requirement": "required",
            }
        ],
        "sub_recipes": _fallback_sub_recipes(),
        "extensions": _AGENT_EXTENSIONS,
        "settings": _AGENT_SETTINGS,
        "response": {"json_schema": _AGENT_RESPONSE_SCHEMA},
    }
    return yaml.safe_dump(recipe, sort_keys=False, default_flow_style=False)
