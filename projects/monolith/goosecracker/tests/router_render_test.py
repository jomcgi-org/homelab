"""Tests for the runtime router renderer.

These assert the generated recipe is valid YAML, is a faithful specialization
of the guest agent.yaml (only enabled sub-recipes, ordered plan, preserved
scaffolding, optional replan escape hatch), and that untrusted per-step
context NEVER reaches the recipe: it lives only in the separate plan file
produced by ``render_plan_file``, which is never templated by goose so it is
safe for arbitrary text including literal `{{ }}`/`{% %}`. A drift guard
reads the checked-in guest agent.yaml and asserts the scaffold tokens we
copied still exist there, so a guest convention change fails this test
loudly.

Under Bazel the guest recipe dir is shipped as test `data` (see the BUILD
entry for goosecracker_router_render_test); outside Bazel it resolves against
the real repo checkout.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from chat.orchestrator_plan import Plan, PlanStep
from goosecracker import router_render

# This test file lives at projects/monolith/goosecracker/tests/, three
# directories below projects/: tests -> goosecracker -> monolith -> projects.
_GUEST_RECIPES = Path(__file__).parents[3] / "firecracker/goosecracker/guest/recipes"
_AGENT_YAML = _GUEST_RECIPES / "agent.yaml"

_ALL_IDS = (
    "query",
    "research",
    "plan",
    "implement",
    "artifact-build",
    "artifact-review",
)


def _loaded(text: str) -> dict:
    """Parse a rendered router and assert it is a YAML mapping (safe_load can
    return None/list/str for a malformed recipe)."""
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise TypeError(f"rendered router is not a YAML mapping: {type(doc)!r}")
    return doc


def _single_step_plan() -> Plan:
    return Plan(
        enabled_subrecipes=("query",),
        steps=(PlanStep(sub_recipe="query", context="Answer how the router works."),),
        done_criteria=("the question is answered",),
    )


def _two_step_plan() -> Plan:
    return Plan(
        enabled_subrecipes=("research", "artifact-build"),
        steps=(
            PlanStep(
                sub_recipe="research",
                context="Find current WHO air-quality guideline PM2.5 limits.",
            ),
            PlanStep(
                sub_recipe="artifact-build",
                context="Build a single-page chart of the researched limits.",
            ),
        ),
        done_criteria=(),
    )


def test_single_step_plan_parses() -> None:
    doc = _loaded(router_render.render_router(_single_step_plan()))
    assert doc["version"]
    assert doc["prompt"]


def test_multi_step_plan_parses() -> None:
    doc = _loaded(router_render.render_router(_two_step_plan()))
    assert doc  # a non-empty mapping


def test_sub_recipes_match_enabled_in_order() -> None:
    plan = _two_step_plan()
    doc = _loaded(router_render.render_router(plan))
    names = [s["name"] for s in doc["sub_recipes"]]
    assert tuple(names) == plan.enabled_subrecipes
    for sub in doc["sub_recipes"]:
        assert sub["path"] == f"/home/goose-agent/recipes/{sub['name']}.yaml"
        assert sub["values"]["task_file"] == "{{ task_file }}"
        assert sub["values"]["context_file"] == "/tmp/goose/context.md"


def test_implement_keeps_sequential_when_repeated() -> None:
    plan = Plan(
        enabled_subrecipes=("implement",),
        steps=(PlanStep(sub_recipe="implement", context="Add a health endpoint."),),
        done_criteria=(),
    )
    doc = _loaded(router_render.render_router(plan))
    sub = doc["sub_recipes"][0]
    assert sub["name"] == "implement"
    assert sub["sequential_when_repeated"] is True


def test_disabled_id_appears_nowhere() -> None:
    plan = _two_step_plan()  # enables research + artifact-build only
    rendered = router_render.render_router(plan)
    enabled = set(plan.enabled_subrecipes)
    disabled = set(_ALL_IDS) - enabled
    # "plan" is the one id we cannot bare-substring check: it is an ordinary
    # English word ("execute the ordered plan") and a substring of the escape
    # hatch field name "replan", so it appears in every rendered router. The
    # real invariant (a disabled sub-recipe is never selectable) still holds:
    # it is not in sub_recipes, the mode enum, or any delegate(source:) target.
    for did in disabled - {"plan"}:
        assert did not in rendered, f"disabled id {did!r} leaked into router"
    # Structural guard covering ALL disabled ids, including "plan": no disabled
    # id may appear as a sub_recipe name or in the mode enum.
    doc = _loaded(rendered)
    sub_names = {s["name"] for s in doc["sub_recipes"]}
    mode_enum = set(doc["response"]["json_schema"]["properties"]["mode"]["enum"])
    for did in disabled:
        assert did not in sub_names, f"disabled id {did!r} is a sub_recipe"
        assert did not in mode_enum, f"disabled id {did!r} is a mode enum value"


def test_response_schema_has_replan_and_required_summary() -> None:
    doc = _loaded(router_render.render_router(_two_step_plan()))
    schema = doc["response"]["json_schema"]
    assert schema["required"] == ["summary"]
    props = schema["properties"]
    assert "replan" in props
    replan = props["replan"]
    assert replan["type"] == "object"
    assert set(replan["properties"]) == {"reason", "what_i_learned", "suggested_focus"}
    # mode/type/url retained; mode enum limited to the enabled ids.
    assert set(props["mode"]["enum"]) == set(_two_step_plan().enabled_subrecipes)
    assert "type" in props
    assert "url" in props


def test_settings_and_scaffolding_preserved() -> None:
    doc = _loaded(router_render.render_router(_two_step_plan()))
    assert doc["settings"]["max_turns"] == 25
    assert doc["settings"]["max_tool_repetitions"] == 5
    assert doc["extensions"] == [{"type": "builtin", "name": "developer"}]
    instr = doc["instructions"]
    assert "::stage" in instr
    assert "recipe__final_output" in instr
    assert "GOOSECRACKER_STEERING_URL" in instr
    # The plan is executed in order, not classified.
    assert "delegate(source:" in instr


def test_steps_reference_plan_file_in_order() -> None:
    # The recipe instructions reference each step's sub_recipe and the plan
    # file, in order, but never the step's own context text (that text is
    # template-unsafe and lives only in render_plan_file's output).
    plan = _two_step_plan()
    doc = _loaded(router_render.render_router(plan))
    instr = doc["instructions"]
    assert router_render._PLAN_FILE in instr
    first = instr.index(plan.steps[0].sub_recipe)
    second = instr.rindex(plan.steps[1].sub_recipe)
    assert first < second  # steps appear in order


def test_recipe_never_contains_step_context() -> None:
    # Template-safety regression (the whole point of this design): untrusted
    # per-step context, including literal `{{ }}` / `{% %}`, must never reach
    # the recipe YAML that goose renders through minijinja before parsing.
    nasty = (
        "- not a list item\n"
        'key: value with a "quote"\n'
        "line two: still text\n"
        "  indented: deeper\n"
        "trailing colon:\n"
        "{{ some.jinja }} and {% a tag %}"
    )
    plan = Plan(
        enabled_subrecipes=("query",),
        steps=(PlanStep(sub_recipe="query", context=nasty),),
        done_criteria=(),
    )
    rendered = router_render.render_router(plan)
    assert nasty not in rendered
    assert "{{ some.jinja }}" not in rendered
    doc = _loaded(rendered)
    assert router_render._PLAN_FILE in doc["instructions"]


def test_render_plan_file_has_one_section_per_step_in_order_with_braces_intact() -> (
    None
):
    # render_plan_file is the safe home for untrusted context: it is plain
    # markdown, never templated, so braces/colons/dashes/quotes survive
    # verbatim. One "## Step <i>: <sub_recipe>" section per step, in order,
    # each followed by that step's context verbatim.
    nasty = (
        "- not a list item\n"
        'key: value with a "quote"\n'
        "line two: still text\n"
        "  indented: deeper\n"
        "trailing colon:\n"
        "{{ some.jinja }} and {% a tag %}"
    )
    plan = Plan(
        enabled_subrecipes=("research", "artifact-build"),
        steps=(
            PlanStep(sub_recipe="research", context=nasty),
            PlanStep(sub_recipe="artifact-build", context="Build the chart."),
        ),
        done_criteria=(),
    )
    plan_md = router_render.render_plan_file(plan)
    assert "## Step 0: research" in plan_md
    assert "## Step 1: artifact-build" in plan_md
    assert plan_md.index("## Step 0: research") < plan_md.index(
        "## Step 1: artifact-build"
    )
    assert nasty in plan_md
    assert "Build the chart." in plan_md
    assert plan_md.index(nasty) < plan_md.index("Build the chart.")


def test_drift_guard_scaffold_tokens_exist_in_guest_agent_yaml() -> None:
    # If the guest changes these conventions, the copied scaffolding in
    # router_render.py is stale and this test must go red.
    text = _AGENT_YAML.read_text()
    assert "::stage::" in text
    assert "recipe__final_output" in text
    assert "GOOSECRACKER_STEERING_URL" in text
