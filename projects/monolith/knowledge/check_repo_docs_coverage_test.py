from pathlib import Path

from knowledge.tools import check_repo_docs_coverage as c


def test_label_to_path_maps_main_repo_source_labels():
    assert c.label_to_path("//:AGENTS.md") == "AGENTS.md"
    assert c.label_to_path("//projects:embervm/README.md") == (
        "projects/embervm/README.md"
    )
    assert c.label_to_path("@@//bazel/helm:README.md") == "bazel/helm/README.md"
    assert c.label_to_path("@//docs:x.md") == "docs/x.md"
    assert c.label_to_path("@@rules_foo+//pkg:README.md") is None
    assert c.label_to_path("WRN something on stdout") is None


def test_missing_docs_reports_uncovered_indexed_paths_only():
    tracked = [
        "AGENTS.md",
        "README.md",  # repo root README is not indexed
        "projects/new-svc/README.md",
        "projects/monolith/ARCHITECTURE.md",
        "projects/x/main.go",
        "projects/x/node_modules/pkg/README.md",
    ]
    covered = [
        "//:AGENTS.md",
        "//projects/monolith:ARCHITECTURE.md",
        "//projects/monolith:extra-untracked.md",
    ]
    assert c.missing_docs(tracked, covered) == ["projects/new-svc/README.md"]


def test_missing_docs_includes_public_docs_selection():
    # Every public doc is also a repo doc today; the check covers both
    # generators' selections so neither can drift out on its own.
    tracked = ["projects/sextant/README.md"]
    assert c.missing_docs(tracked, []) == ["projects/sextant/README.md"]


def test_main_fails_on_empty_query_output(tmp_path: Path):
    labels = tmp_path / "labels.txt"
    labels.write_text("INF bb sidecar noise\n")
    assert c.main([str(labels)]) == 1


def test_main_usage_error():
    assert c.main([]) == 2
