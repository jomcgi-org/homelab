"""Guard the staged revision-aware health gate (#4745).

templates/promotion.yaml renders one Stage per pipeline stage in values.yaml,
and the promotion steps are the whole gate: Kargo `verification` is off because
Argo Rollouts is off. Two things about the revision gate are worth pinning
because either regresses silently:

  1. It is OFF by default. `promotion.revisionGate.enabled: false` must leave
     the default render identical to a render with no gate at all, because the
     monolith's /healthz does not carry `chart_version` yet, and rendering the
     step early would make every production promotion poll until timeout and
     fail. A refactor that drops the `and` in the template guard turns the
     flag into decoration.

  2. When ON, a Stage that carries `revisionURL` and no `verdictURL` (so no
     conformance runner) still gets the `http` step, placed after argocd-wait,
     with a successExpression keyed on `chart_version` and no
     failureExpression. The conformance step and this one share a template
     block, so a change to one can drop the other.

Both renders need helm: HELM_BIN comes from the BUILD target under Bazel and
falls back to `helm` on PATH locally. values.yaml sits beside Chart.yaml, in the
repo and in the runfiles tree alike.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

RELEASE = "kargo"


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _render(extra: list[str] | None = None) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        RELEASE,
        str(_chart_dir()),
        "--namespace",
        RELEASE,
        "--values",
        str(_chart_dir() / "values.yaml"),
        *(extra or []),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _stages(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {
        (d["metadata"]["namespace"], d["metadata"]["name"]): d
        for d in docs
        if d.get("kind") == "Stage"
    }


def _steps(stage: dict) -> list[dict]:
    return stage["spec"]["promotionTemplate"]["spec"]["steps"]


def _http_steps(stage: dict) -> list[dict]:
    return [s for s in _steps(stage) if s["uses"] == "http"]


def _values() -> dict:
    with open(_chart_dir() / "values.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _stages_with(key: str) -> list[tuple[str, str]]:
    """(project, stage name) for every values.yaml stage carrying `key`."""
    return [
        (pipeline["project"], stage["name"])
        for pipeline in _values()["promotion"]["pipelines"]
        for stage in pipeline["stages"]
        if stage.get(key)
    ]


def test_gate_is_wired_but_off_by_default():
    values = _values()
    assert values["promotion"]["revisionGate"]["enabled"] is False, (
        "promotion.revisionGate.enabled flipped on in values.yaml: the monolith "
        "/healthz must carry chart_version before this is safe (#4745)"
    )
    assert ("kargo-monolith", "prod") in _stages_with("revisionURL"), (
        "the monolith prod stage no longer carries revisionURL, so enabling "
        "the gate would render nothing for production"
    )


def test_default_render_carries_no_rollout_version_step():
    """The flag is the only thing standing between the wired revisionURL and a
    step that fails every promotion until the workload exposes chart_version,
    so the default render must not contain it, on any Stage."""
    for key, stage in _stages(_render()).items():
        names = [s.get("as") for s in _http_steps(stage)]
        assert "rollout-version" not in names, (
            f"{key} renders the revision gate with revisionGate.enabled false"
        )


def test_default_render_equals_render_with_no_revision_urls():
    """Stronger than the step being absent: with the flag off, revisionURL
    and verdictTimeout on a stage must change nothing, so the wiring is
    provably inert rather than merely unexercised."""
    baseline = _render()
    stripped = _render(
        [
            f"--set=promotion.pipelines[{i}].stages[{j}].revisionURL=null"
            for i, pipeline in enumerate(_values()["promotion"]["pipelines"])
            for j, stage in enumerate(pipeline["stages"])
            if stage.get("revisionURL")
        ]
    )
    assert _stages(baseline) == _stages(stripped)


def test_enabled_render_carries_step_on_stage_without_conformance():
    """A Stage with revisionURL and no verdictURL still renders the http step:
    the conformance runner is not a prerequisite for the revision gate."""
    stages = _stages(_render(["--set", "promotion.revisionGate.enabled=true"]))
    prod = stages[("kargo-monolith", "prod")]
    assert "verdictURL" not in yaml.safe_dump(prod)
    assert [s["uses"] for s in _steps(prod)] == ["argocd-update", "argocd-wait", "http"]

    (step,) = _http_steps(prod)
    assert step["as"] == "rollout-version"
    assert step["config"]["url"].endswith("/healthz")
    assert "chart_version" in step["config"]["successExpression"]
    assert "response.status == 200" in step["config"]["successExpression"]
    # Kargo pre-evaluates the desiredRevision expression so the body can only
    # match the Freight's own chart version.
    assert "chartFrom(" in step["config"]["successExpression"]
    # No fail-fast: a stale chart_version is the expected state right after a
    # roll, and only the retry timeout may fail the Promotion.
    assert "failureExpression" not in step["config"]
    assert step["retry"]["errorThreshold"] == 3
    assert step["retry"]["timeout"] == "10m0s"


def test_enabled_render_leaves_conformance_stages_unchanged():
    """The embervm dev stage carries verdictURL and no revisionURL, so turning
    the flag on must not add a second http step there."""
    off = _stages(_render())
    on = _stages(_render(["--set", "promotion.revisionGate.enabled=true"]))
    for key in _stages_with("verdictURL"):
        assert key in off, f"{key} missing from the render"
        assert _steps(off[key]) == _steps(on[key]), f"{key} changed with the flag"


def test_monolith_revision_url_targets_the_probe_port():
    """The gate must ask the same /healthz the readiness probe already hits,
    on the api port (service.apiPort 8000), of the release-named Service in
    the Application's destination namespace, or it proves something other
    than the probe's Healthy. Derived from projects/gke-apps/monolith and the
    monolith chart's service.yaml, not guessed."""
    stages = _stages(_render(["--set", "promotion.revisionGate.enabled=true"]))
    (step,) = _http_steps(stages[("kargo-monolith", "prod")])
    assert (
        step["config"]["url"]
        == "http://monolith.monolith.svc.cluster.local:8000/healthz"
    )
