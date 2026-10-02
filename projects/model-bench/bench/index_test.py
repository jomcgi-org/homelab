import json

import pytest

from bench.cache import HARNESS_VERSION
from bench.index import (
    IndexConfig,
    axis_scores,
    build_index,
    load_config,
    load_judge_ratings,
    render_index_markdown,
    role_index,
)
from bench.schema import Attempt, ResultCell, TaskSpec, VerifierSpec

TIER_OF = {
    "floor-a": "standard",
    "floor-b": "easy",
    "hard-a": "hard",
    "conflict": "hard",
}
AXES_OF = {"conflict": ["judgement"]}


def _cell(task_id, model_id, passed, *, score=None, norms=None, error=False):
    return ResultCell(
        task_id=task_id,
        task_version="v1",
        model_id=model_id,
        content_hash="h",
        outcome="pass@1" if passed else "fail",
        attempts=[
            Attempt(
                passed=passed,
                score=score,
                feedback="[harness error] boom" if error else "",
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=0,
            )
        ],
        cost_usd=0.01,
        harness_version=HARNESS_VERSION,
        prompt_template_hash="agent",
        turns=1,
        tool_use_ok=True,
        norms={"norms_score": norms} if norms is not None else None,
    )


def _config(**roles):
    return IndexConfig(roles=roles or {"r": {"correctness": 1.0}}, bootstrap=50)


def test_axis_scores_split_by_tier_and_tag():
    cells = [
        _cell("floor-a", "m", True, norms=0.9),
        _cell("floor-b", "m", False),
        _cell("hard-a", "m", False, score=0.5),
        _cell("conflict", "m", True),
    ]
    out = axis_scores(cells, TIER_OF, AXES_OF)
    assert out["correctness"] == (0.5, 2)
    # Hard tasks count toward frontier whether or not they are also tagged.
    assert out["frontier"] == (0.75, 2)
    assert out["judgement"] == (1.0, 1)
    assert out["security"] == (None, 0)
    # Norms only from passing cells that carry a score.
    assert out["norms"] == (0.9, 1)


def test_role_index_renormalises_missing_axes():
    value, coverage = role_index(
        {"correctness": 1.0, "judgement": None, "frontier": 0.5},
        {"correctness": 0.5, "judgement": 0.25, "frontier": 0.25},
    )
    assert value == pytest.approx((1.0 * 0.5 + 0.5 * 0.25) / 0.75)
    assert coverage == pytest.approx(0.75)
    assert role_index({}, {"correctness": 1.0}) == (None, 0.0)


def test_build_index_picks_cheapest_and_fastest_near_best():
    groups = {
        "pricey": [_cell("floor-a", "pricey", True), _cell("floor-b", "pricey", True)],
        "cheap": [_cell("floor-a", "cheap", True), _cell("floor-b", "cheap", True)],
        "weak": [_cell("floor-a", "weak", False), _cell("floor-b", "weak", False)],
    }
    stats = {
        "pricey": {"cost": 1.0, "mean_latency_ms": 100.0},
        "cheap": {"cost": 0.1, "mean_latency_ms": 900.0},
        "weak": {"cost": 0.001, "mean_latency_ms": 1.0},
    }
    block = build_index(
        groups,
        tier_of=TIER_OF,
        axes_of=AXES_OF,
        config=_config(implementer={"correctness": 1.0}),
        stats=stats,
        anchor_ids={"pricey"},
    )
    picks = block["picks"]["implementer"]
    # Both strong models tie at 1.0; the weak one is outside the tolerance.
    assert picks["best"] in {"pricey", "cheap"}
    assert picks["cheapest"] == "cheap"
    assert picks["fastest"] == "pricey"
    assert block["models"]["pricey"]["role"] == "anchor"
    # n=2 floor tasks is below the default min_n of 3.
    assert block["models"]["cheap"]["low_n"] == ["correctness"]
    ci = block["models"]["weak"]["roles"]["implementer"]["ci"]
    assert ci == [0.0, 0.0]


def test_build_index_skips_harness_errors_and_uses_judge():
    groups = {
        "a": [_cell("floor-a", "a", True), _cell("floor-b", "a", False, error=True)],
        "b": [_cell("floor-a", "b", False)],
    }
    block = build_index(
        groups,
        tier_of=TIER_OF,
        axes_of=AXES_OF,
        config=_config(planner={"correctness": 0.5, "judge": 0.5}),
        judge_ratings={"a": 1200.0, "b": 1000.0},
    )
    a = block["models"]["a"]
    assert a["axis_n"]["correctness"] == 1
    assert a["axes"]["judge"] == 1.0 and block["models"]["b"]["axes"]["judge"] == 0.0
    assert a["roles"]["planner"]["index"] == pytest.approx(1.0)
    assert block["judge"] is True


def test_load_config_rejects_unknown_axes(tmp_path):
    p = tmp_path / "index.yaml"
    p.write_text("roles:\n  r:\n    vibes: 1\n")
    with pytest.raises(ValueError, match="vibes"):
        load_config(p)


def test_repo_index_yaml_loads():
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "index.yaml"
    if not path.exists():
        pytest.skip("index.yaml is not a runfile under Bazel")
    cfg = load_config(path)
    assert set(cfg.roles) == {"planner", "implementer", "reviewer"}


def test_load_judge_ratings_accepts_both_shapes(tmp_path):
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"a": 1100, "b": {"judge_rating": 900}, "c": None}))
    assert load_judge_ratings(flat) == {"a": 1100.0, "b": 900.0}
    board = tmp_path / "board.json"
    board.write_text(
        json.dumps({"models": [{"id": "a", "judge_rating": 1.5}, {"id": "b"}]})
    )
    assert load_judge_ratings(board) == {"a": 1.5}


def test_render_index_markdown_lists_roles():
    groups = {"m": [_cell("floor-a", "m", True)]}
    block = build_index(
        groups, tier_of=TIER_OF, axes_of=AXES_OF, config=_config(r={"correctness": 1})
    )
    md = render_index_markdown(block, {"m": "Model M"})
    assert "### r" in md and "| Model M | 1.000 |" in md


def test_task_spec_axes_field():
    spec = TaskSpec.model_validate(
        {
            "id": "t",
            "version": "v1",
            "class": "code-fix",
            "prompt": "p",
            "verifier": VerifierSpec(kind="pytest"),
            "axes": ["judgement", "security"],
        }
    )
    assert spec.axes == ["judgement", "security"]
    with pytest.raises(ValueError):
        TaskSpec.model_validate(
            {
                "id": "t",
                "version": "v1",
                "class": "code-fix",
                "prompt": "p",
                "verifier": VerifierSpec(kind="pytest"),
                "axes": ["vibes"],
            }
        )
