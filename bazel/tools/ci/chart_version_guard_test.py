"""Tests for the chart version guard (ADR platform/009 decision 1)."""

from __future__ import annotations

import chart_version_guard as guard

CHART_DIR = "projects/svc/chart"
CHART = f"{CHART_DIR}/Chart.yaml"
BUILD = f"{CHART_DIR}/BUILD"
APP = "projects/svc/deploy/application.yaml"

PUBLISHED_BUILD = 'helm_chart(\n    name = "chart",\n    publish = True,\n)\n'
LOCAL_BUILD = 'helm_chart(\n    name = "chart",\n)\n'

OURS = """\
spec:
  sources:
    - repoURL: ghcr.io/jomcgi/homelab/charts
      chart: svc
      targetRevision: 0.10.0
    - repoURL: https://github.com/jomcgi-org/homelab.git
      targetRevision: HEAD
      ref: values
"""
HEAD_ONLY = OURS.replace("targetRevision: 0.10.0", "targetRevision: HEAD")

CHART_V1 = "apiVersion: v2\nname: svc\nversion: 0.1.0\n"
CHART_V2 = "apiVersion: v2\nname: svc\nversion: 0.2.0\n"


def _run(base: dict[str, str], head: dict[str, str]) -> list[str]:
    trees = {"base": base, "head": head}

    def read(ref: str, path: str) -> str | None:
        return trees[ref].get(path)

    published = guard.published_chart_dirs(base, read, "base") | (
        guard.published_chart_dirs(head, read, "head")
    )
    changed = sorted(p for p in set(base) | set(head) if base.get(p) != head.get(p))
    return guard.findings(changed, published, read, "base", "head")


def _tree(app: str = OURS, chart: str = CHART_V1, build: str = PUBLISHED_BUILD):
    return {APP: app, CHART: chart, BUILD: build}


def test_raising_target_revision_is_rejected():
    [problem] = _run(_tree(), _tree(app=OURS.replace("0.10.0", "0.11.0")))
    assert APP in problem and "0.11.0" in problem


def test_numeric_not_lexical_comparison():
    [problem] = _run(_tree(), _tree(app=OURS.replace("0.10.0", "0.100.0")))
    assert "0.100.0" in problem


def test_lowering_target_revision_is_the_revert_lever():
    assert _run(_tree(), _tree(app=OURS.replace("0.10.0", "0.9.0"))) == []


def test_changing_published_chart_version_is_rejected():
    [problem] = _run(_tree(), _tree(chart=CHART_V2))
    assert CHART in problem


def test_unpublished_chart_is_out_of_scope():
    base = _tree(build=LOCAL_BUILD)
    head = _tree(
        build=LOCAL_BUILD, chart=CHART_V2, app=OURS.replace("0.10.0", "0.11.0")
    )
    assert _run(base, head) == []


def test_moving_onto_the_registry_is_allowed():
    assert _run(_tree(app=HEAD_ONLY), _tree()) == []


def test_moving_off_the_registry_is_allowed():
    assert _run(_tree(), _tree(app=HEAD_ONLY)) == []


def test_other_edits_are_allowed():
    head = _tree(
        app=OURS.replace("chart: svc", "chart: svc\n      helm: {}"),
        chart=CHART_V1 + "description: x\n",
    )
    assert _run(_tree(), head) == []


def test_dependency_versions_are_not_the_chart_version():
    chart = CHART_V1 + "dependencies:\n  - name: dep\n    version: 9.9.9\n"
    assert _run(_tree(chart=chart), _tree(chart=chart.replace("9.9.9", "9.9.10"))) == []


def test_new_service_may_seed_its_first_version():
    assert _run({}, _tree()) == []


def test_deleting_a_service_is_allowed():
    assert _run(_tree(), {}) == []


def test_chart_outside_a_chart_directory_is_covered():
    chart_dir = "projects/op/helm/op-operator"
    base = {f"{chart_dir}/Chart.yaml": CHART_V1, f"{chart_dir}/BUILD": PUBLISHED_BUILD}
    head = {**base, f"{chart_dir}/Chart.yaml": CHART_V2}
    [problem] = _run(base, head)
    assert chart_dir in problem


def test_app_the_bot_never_writes_is_out_of_scope():
    other = "projects/svc/dev/deploy/application.yaml"
    base = {**_tree(), other: OURS}
    head = {**_tree(), other: OURS.replace("0.10.0", "0.11.0")}
    assert _run(base, head) == []


def test_app_colocated_in_chart_dir_is_covered():
    chart_dir = "projects/op/helm/op"
    app = f"{chart_dir}/application.yaml"
    base = {
        f"{chart_dir}/Chart.yaml": CHART_V1,
        f"{chart_dir}/BUILD": PUBLISHED_BUILD,
        app: OURS,
    }
    head = {**base, app: OURS.replace("0.10.0", "0.11.0")}
    [problem] = _run(base, head)
    assert app in problem
