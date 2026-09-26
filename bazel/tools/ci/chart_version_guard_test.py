"""Tests for the chart version guard (ADR platform/009 decision 1)."""

from __future__ import annotations

import chart_version_guard as guard

APP = "projects/svc/deploy/application.yaml"
CHART = "projects/svc/chart/Chart.yaml"

OURS = """\
spec:
  sources:
    - repoURL: ghcr.io/jomcgi/homelab/charts
      chart: svc
      targetRevision: 0.1.0
    - repoURL: https://github.com/jomcgi-org/homelab.git
      targetRevision: HEAD
      ref: values
"""

UPSTREAM = """\
spec:
  source:
    repoURL: https://charts.example.com
    chart: thing
    targetRevision: 1.2.3
"""

CHART_V1 = "apiVersion: v2\nname: svc\nversion: 0.1.0\n"
CHART_V2 = "apiVersion: v2\nname: svc\nversion: 0.2.0\n"


def _reader(base: dict[str, str], head: dict[str, str]):
    trees = {"base": base, "head": head}
    return lambda ref, path: trees[ref].get(path)


def _run(base: dict[str, str], head: dict[str, str]) -> list[str]:
    changed = sorted(set(base) | set(head))
    return guard.findings(changed, _reader(base, head), "base", "head")


def test_bumping_our_target_revision_is_rejected():
    head = OURS.replace("0.1.0", "0.2.0")
    [problem] = _run({APP: OURS}, {APP: head})
    assert APP in problem and "0.2.0" in problem


def test_bumping_our_chart_version_is_rejected():
    [problem] = _run({APP: OURS, CHART: CHART_V1}, {APP: OURS, CHART: CHART_V2})
    assert CHART in problem


def test_other_edits_to_our_files_are_allowed():
    head = OURS.replace("chart: svc", "chart: svc\n      helm: {}")
    chart = CHART_V1 + "description: x\n"
    assert _run({APP: OURS, CHART: CHART_V1}, {APP: head, CHART: chart}) == []


def test_upstream_pins_are_out_of_scope():
    head = UPSTREAM.replace("1.2.3", "1.3.0")
    assert _run({APP: UPSTREAM}, {APP: head}) == []


def test_chart_without_registry_app_is_out_of_scope():
    assert _run({APP: UPSTREAM, CHART: CHART_V1}, {APP: UPSTREAM, CHART: CHART_V2}) == []


def test_new_service_may_seed_its_first_version():
    assert _run({}, {APP: OURS, CHART: CHART_V1}) == []


def test_deleting_a_service_is_allowed():
    assert _run({APP: OURS, CHART: CHART_V1}, {}) == []


def test_dependency_versions_are_not_the_chart_version():
    chart = CHART_V1 + "dependencies:\n  - name: dep\n    version: 9.9.9\n"
    bumped = chart.replace("9.9.9", "9.9.10")
    assert _run({APP: OURS, CHART: chart}, {APP: OURS, CHART: bumped}) == []
