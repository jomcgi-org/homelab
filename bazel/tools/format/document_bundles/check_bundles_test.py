from pathlib import Path, PurePosixPath as P

import pytest
import yaml

from check_bundles import validate, report, shape

RULES = yaml.safe_load(Path("docs/templates/bundles.yaml").read_text())
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
