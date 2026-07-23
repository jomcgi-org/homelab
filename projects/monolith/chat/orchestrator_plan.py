"""Typed ``submit_plan`` schema, deserializer, and semantic validator.

ADR 036 has the DeepSeek orchestrator select sub-recipes and order them with
per-step context, never author recipe YAML itself (Design invariant 1 of
the DeepSeek runtime recipes design). This module is the
typed boundary that makes that invariant mechanical:

- ``submit_plan_schema()`` builds a JSON Schema for a ``submit_plan`` tool
  whose enums are sourced from ``goosecracker.recipe_catalog.enabled_enum()``,
  so a non-catalog sub-recipe id is structurally unrepresentable.
- ``plan_from_dict()`` deserializes the tool-call arguments JSON object into
  a ``Plan``.
- ``validate_plan()`` catches the semantic errors typing cannot: an empty
  step list, blank context, or a step naming a sub-recipe that was not
  included in ``enabled_subrecipes``. A non-empty error list forces the
  fail-open path in ``orchestrator.compile`` (Task 5).

The probe at ``scratchpad/probe_submit_plan.py`` validated this shape via a
forced tool call (``tools`` + ``tool_choice``) at 12/12 across trials.
``response_format: {"type": "json_schema", ...}`` (also 12/12 in the probe)
is the documented drop-in fallback mechanism if tool-calling ever regresses
for the pinned model; see ``orchestrator_client.call_tool``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from goosecracker.api import CATALOG, enabled_enum


@dataclass(frozen=True)
class PlanStep:
    """One ordered step in a runtime plan: a sub-recipe id plus the context
    that step needs."""

    sub_recipe: str
    context: str


@dataclass(frozen=True)
class Plan:
    """A DeepSeek-authored runtime plan: the allow-set of sub-recipes, the
    ordered steps that use them, and the done criteria for the session."""

    enabled_subrecipes: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    done_criteria: tuple[str, ...] = field(default_factory=tuple)


def submit_plan_schema() -> dict:
    """Build the JSON Schema for the ``submit_plan`` tool.

    Both enums (``enabled_subrecipes.items`` and
    ``steps.items.properties.sub_recipe``) are sourced from
    ``recipe_catalog.enabled_enum()`` so a non-catalog sub-recipe id is
    unrepresentable in the tool call.
    """
    ids = enabled_enum()
    descriptions = "; ".join(f"{CATALOG[i].id} = {CATALOG[i].description}" for i in ids)

    return {
        "name": "submit_plan",
        "description": "Submit the delegation plan for this task.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled_subrecipes", "steps", "done_criteria"],
            "properties": {
                "enabled_subrecipes": {
                    "type": "array",
                    "items": {"type": "string", "enum": ids},
                    "description": (
                        "Allow-set of sub-recipes this plan may use. "
                        f"Sub-recipe meanings: {descriptions}"
                    ),
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["sub_recipe", "context"],
                        "properties": {
                            "sub_recipe": {
                                "type": "string",
                                "enum": ids,
                                "description": (
                                    "The sub-recipe this step delegates to. "
                                    f"Sub-recipe meanings: {descriptions}"
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "What this step should do and the context "
                                    "it needs. Must be non-empty."
                                ),
                            },
                        },
                    },
                    "description": (
                        "Ordered sequence goose executes. Each step names "
                        "exactly one sub-recipe."
                    ),
                },
                "done_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "How to tell the session is complete.",
                },
            },
        },
    }


def plan_from_dict(args: dict) -> Plan:
    """Deserialize a ``submit_plan`` tool-call arguments object into a Plan.

    Tolerates missing optional fields: ``done_criteria`` defaults to an empty
    tuple. Coerces defensively so a malformed model payload can never yield a
    ``None`` or unhashable value: the JSON Schema constrains a field's TYPE but
    not its nullability, so a model can still emit ``"context": null`` or a
    ``sub_recipe`` that is an object. Everything is coerced to ``str`` here, so
    such a payload deserializes without raising and instead produces a Plan that
    :func:`validate_plan` then rejects (the correct fail-open path), rather than
    an ``AttributeError``/``TypeError`` escaping into ``compile``.
    """
    enabled = tuple(str(x) for x in (args.get("enabled_subrecipes") or ()))
    steps = tuple(
        PlanStep(
            sub_recipe=str(step.get("sub_recipe") or ""),
            context=str(step.get("context") or ""),
        )
        for step in (args.get("steps") or ())
    )
    done_criteria = tuple(str(x) for x in (args.get("done_criteria") or ()))
    return Plan(enabled_subrecipes=enabled, steps=steps, done_criteria=done_criteria)


def validate_plan(plan: Plan) -> list[str]:
    """Return a list of human-readable semantic errors; empty means valid.

    Typing (the JSON Schema enum) already rules out non-catalog ids reaching
    here in the happy path, but this validator does not trust that: it is
    the last line of defense before a plan is rendered into a router recipe,
    so it re-checks catalog membership directly.
    """
    errors: list[str] = []
    catalog_ids = set(CATALOG)

    if not plan.steps:
        errors.append("plan has no steps")

    for sub_recipe in plan.enabled_subrecipes:
        if sub_recipe not in catalog_ids:
            errors.append(f"enabled_subrecipes has non-catalog id: {sub_recipe!r}")

    enabled_set = set(plan.enabled_subrecipes)
    for i, step in enumerate(plan.steps):
        if step.sub_recipe not in catalog_ids:
            errors.append(f"step[{i}].sub_recipe non-catalog: {step.sub_recipe!r}")
        elif step.sub_recipe not in enabled_set:
            errors.append(
                f"step[{i}].sub_recipe {step.sub_recipe!r} not in enabled_subrecipes"
            )
        if not step.context.strip():
            errors.append(f"step[{i}].context is empty")

    return errors
