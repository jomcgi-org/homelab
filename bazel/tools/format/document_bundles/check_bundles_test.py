import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath as P

import pytest

from check_bundles import validate, report, shape

RULES = json.loads(Path("docs/templates/bundles.yaml").read_text())
ARCH = P("projects/api/ARCHITECTURE.md")
README = ARCH.with_name("README.md")
GOOD = "```mermaid\ngraph LR\n```\n## Direction\n| Component | Untrusted input | Credentials held |\n| --- | --- | --- |\n| worker | none | none |"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (GOOD.replace("mermaid", "text"), "opening_fence"),
        ("## First\n" + GOOD, "opening_fence"),
        (GOOD.replace("## Direction", "### Direction"), "heading"),
        (GOOD.replace("Untrusted input", "Input"), "table_columns"),
        (GOOD.replace("Credentials held", "Credentials"), "table_columns"),
        (GOOD + "\n" * 601, "max_lines"),
        (GOOD + "\nStatus (today)", "forbidden_prefixes"),
        (GOOD + "\n**Status (today)", "forbidden_prefixes"),
    ],
)
def test_architecture(text, rule):
    rules = RULES["format"][ARCH.name]
    assert not list(shape(ARCH, GOOD, rules, {}))
    assert [e.split(":")[0] for e in shape(ARCH, text, rules, {})] == [rule]


def test_service_and_component():
    child = ARCH.parent / "worker/ARCHITECTURE.md"
    docs = {
        ARCH: GOOD + "\n[worker](worker/ARCHITECTURE.md)",
        child: GOOD,
        README: "[design](./ARCHITECTURE.md)",
    }
    files = set(docs) | {
        ARCH.parent / n
        for n in ["worker/src/main.go", "tests/test.go", "chart/Chart.yaml"]
    }
    assert not validate(RULES, files, docs)
    docs[ARCH] = GOOD.replace("| worker |", "| workers |")
    docs[README] = "ARCHITECTURE.md"
    errors = validate(RULES, files, docs)
    assert "missing component row for worker" in errors[ARCH]
    assert "link_if_present" in errors[README][0]
    assert "missing link from" in errors[child][0]


def test_project_service_alternative_and_names():
    other = P("projects/core/ARCHITECTURE.md")
    bad = ARCH.parent / "src/NOTES.md"
    files = {bad, ARCH.parent / "chart/Chart.yaml", other}
    errors = validate(RULES, files, {bad: ""})
    assert errors[README] == ["missing required file"]
    assert bad in errors and ARCH in errors
    files.add(README)
    for target, valid in [
        ("../core/ARCHITECTURE.md", True),
        ("../absent/ARCHITECTURE.md", False),
    ]:
        errors = validate(RULES, files, {README: f"[design]({target})"})
        assert (ARCH not in errors) == valid


def test_collection_and_exemptions():
    path = P("projects/platform/README.md")
    files = {path, path.parent / "gateway/app.yaml"}
    errors = validate(RULES, files, {path: "gateways"})
    assert errors[path] == ["missing child gateway"]
    assert not list(report(errors, {path}))
    assert list(report(validate(RULES, files, {path: "gateway"}), {path})) == [
        f"{path}: remove stale exemption"
    ]


@pytest.mark.parametrize(
    ("usage", "passes"),
    [
        ("[Architecture][Design]", True),
        ("[design][]", True),
        ("[design]", True),
        ("[design](elsewhere.md)", False),
        ("![design][]", False),
        ("unused definition", False),
    ],
)
def test_reference_links(usage, passes):
    text = usage + '\n\n[design]: <./ARCHITECTURE.md#direction> "Service design"'
    errors = list(shape(README, text, RULES["format"][README.name], {ARCH}))
    assert (not errors) == passes


def test_architecture_template():
    template = P("docs/templates/ARCHITECTURE.md")
    errors = validate(RULES, {template}, {template: Path(template).read_text()})
    assert not errors


def test_lint_with_preexisting_tools_cache(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    checker = Path("bazel/tools/format/document_bundles/check_bundles.py")
    for source in (checker, Path("docs/templates/bundles.yaml")):
        dest = checkout / source
        dest.parent.mkdir(parents=True)
        shutil.copyfile(source, dest)
    (checkout / checker.parent / "exemptions.txt").touch()
    project = checkout / "projects/sample"
    project.mkdir(parents=True)
    (project / "main.py").touch()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=checkout,
        check=True,
    )
    cache = tmp_path / "cache"
    python = cache / "homelab-tools/usr/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nexec "$FIXTURE_PYTHON" -IS "$@"\n')
    python.chmod(0o755)
    env = dict(
        os.environ,
        XDG_CACHE_HOME=str(cache),
        CI_BASE_REF="HEAD",
        FIXTURE_PYTHON=sys.executable,
        CI_REQUIRE_COMPLETE="",
    )
    command = ["bash", str(Path("bazel/tools/ci/ci").resolve()), "lint"]
    missing = subprocess.run(
        command, cwd=checkout, env=env, capture_output=True, text=True
    )
    assert missing.returncode != 0
    assert "projects/sample/README.md: missing required file" in missing.stdout
    (project / "README.md").touch()
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "document project",
        ],
        cwd=checkout,
        check=True,
    )
    subprocess.run(command, cwd=checkout, env=env, check=True)
