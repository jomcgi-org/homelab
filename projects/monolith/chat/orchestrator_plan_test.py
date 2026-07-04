"""Tests for chat.orchestrator_plan (ADR 036 runtime-recipe amendment)."""

from goosecracker import recipe_catalog

from chat.orchestrator_plan import (
    Plan,
    PlanStep,
    plan_from_dict,
    submit_plan_schema,
    validate_plan,
)

_CATALOG_IDS = set(recipe_catalog.enabled_enum())


def _good_args() -> dict:
    return {
        "enabled_subrecipes": ["query", "implement"],
        "steps": [
            {"sub_recipe": "query", "context": "Find the README h1 heading."},
            {"sub_recipe": "implement", "context": "Fix the typo and open a PR."},
        ],
        "done_criteria": ["PR opened with the typo fixed"],
    }


class TestSubmitPlanSchema:
    def test_enum_matches_catalog_in_both_locations(self):
        schema = submit_plan_schema()
        params = schema["parameters"]
        enabled_enum = params["properties"]["enabled_subrecipes"]["items"]["enum"]
        step_enum = params["properties"]["steps"]["items"]["properties"]["sub_recipe"][
            "enum"
        ]
        assert set(enabled_enum) == _CATALOG_IDS
        assert set(step_enum) == _CATALOG_IDS

    def test_name_is_submit_plan(self):
        assert submit_plan_schema()["name"] == "submit_plan"

    def test_steps_require_min_items_and_no_additional_properties(self):
        params = submit_plan_schema()["parameters"]
        assert params["additionalProperties"] is False
        assert params["properties"]["steps"]["minItems"] == 1
        step_schema = params["properties"]["steps"]["items"]
        assert step_schema["additionalProperties"] is False
        assert set(step_schema["required"]) == {"sub_recipe", "context"}

    def test_done_criteria_present(self):
        params = submit_plan_schema()["parameters"]
        assert params["properties"]["done_criteria"]["type"] == "array"
        assert "done_criteria" in params["required"]


class TestPlanFromDict:
    def test_round_trips_representative_args(self):
        plan = plan_from_dict(_good_args())
        assert plan == Plan(
            enabled_subrecipes=("query", "implement"),
            steps=(
                PlanStep(sub_recipe="query", context="Find the README h1 heading."),
                PlanStep(sub_recipe="implement", context="Fix the typo and open a PR."),
            ),
            done_criteria=("PR opened with the typo fixed",),
        )

    def test_missing_done_criteria_defaults_empty(self):
        args = _good_args()
        del args["done_criteria"]
        plan = plan_from_dict(args)
        assert plan.done_criteria == ()

    def test_missing_steps_and_enabled_default_empty(self):
        plan = plan_from_dict({})
        assert plan.enabled_subrecipes == ()
        assert plan.steps == ()
        assert plan.done_criteria == ()

    def test_malformed_payload_does_not_raise_and_fails_validation(self):
        """A malformed tool payload (the JSON Schema constrains type but not
        nullability) must deserialize without raising and instead yield a plan
        that validate_plan rejects (the fail-open path), never a None/unhashable
        value that escapes as AttributeError/TypeError. Covers a null context, a
        sub_recipe that is an object, and a non-str enabled_subrecipes item."""
        args = {
            "enabled_subrecipes": ["query", {"unhashable": 1}],
            "steps": [
                {"sub_recipe": "query", "context": None},
                {"sub_recipe": {"nested": "obj"}, "context": "do it"},
            ],
            "done_criteria": [None],
        }
        plan = plan_from_dict(args)  # must not raise
        # Every field coerced to str: no None, no dict, so validate_plan can run.
        assert all(isinstance(x, str) for x in plan.enabled_subrecipes)
        assert all(
            isinstance(s.sub_recipe, str) and isinstance(s.context, str)
            for s in plan.steps
        )
        # The coerced garbage is rejected by the validator (fail-open path).
        assert validate_plan(plan) != []


class TestValidatePlan:
    def test_accepts_good_plan(self):
        plan = plan_from_dict(_good_args())
        assert validate_plan(plan) == []

    def test_rejects_empty_steps(self):
        plan = Plan(enabled_subrecipes=("query",), steps=())
        errors = validate_plan(plan)
        assert any("no steps" in e for e in errors)

    def test_rejects_empty_context(self):
        plan = Plan(
            enabled_subrecipes=("query",),
            steps=(PlanStep(sub_recipe="query", context="   "),),
        )
        errors = validate_plan(plan)
        assert any("context is empty" in e for e in errors)

    def test_rejects_non_catalog_sub_recipe(self):
        plan = Plan(
            enabled_subrecipes=("query",),
            steps=(PlanStep(sub_recipe="not-a-recipe", context="do it"),),
        )
        errors = validate_plan(plan)
        assert any("non-catalog" in e for e in errors)

    def test_rejects_stepped_but_not_enabled(self):
        plan = Plan(
            enabled_subrecipes=("query",),
            steps=(PlanStep(sub_recipe="implement", context="do it"),),
        )
        errors = validate_plan(plan)
        assert any("not in enabled_subrecipes" in e for e in errors)
