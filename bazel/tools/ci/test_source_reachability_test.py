"""Regression tests for tracked Python test source reachability."""

from __future__ import annotations

import pytest

import test_source_reachability as reachability


def _xml(*elements: str) -> str:
    return "<query version='2'>" + "".join(elements) + "</query>"


def _source(label: str) -> str:
    return f"<source-file name='{label}'/>"


def _rule(
    label: str,
    *,
    rule_class: str,
    srcs: tuple[str, ...] = (),
    data: tuple[str, ...] = (),
) -> str:
    src_labels = "".join(f"<label value='{source}'/>" for source in srcs)
    data_labels = "".join(f"<label value='{source}'/>" for source in data)
    return (
        f"<rule class='{rule_class}' name='{label}'>"
        f"<list name='srcs'>{src_labels}</list>"
        f"<list name='data'>{data_labels}</list>"
        "</rule>"
    )


def test_direct_source_from_evaluated_macro_or_glob_passes():
    graph = reachability.parse_query_graph(
        _xml(
            _rule(
                "//app:api_test",
                rule_class="py_test",
                srcs=("//app:api_test.py",),
            ),
            _source("//app:api_test.py"),
        )
    )

    assert (
        reachability.find_orphans(["app/api_test.py"], ["//app:api_test"], graph) == ()
    )


def test_repository_used_indirect_source_wrapper_passes():
    graph = reachability.parse_query_graph(
        _xml(
            _rule(
                "//app:wrapped_test",
                rule_class="py_test",
                srcs=("//app:test_sources",),
            ),
            _rule(
                "//app:test_sources",
                rule_class="filegroup",
                srcs=("//app:wrapped_test.py",),
            ),
            _source("//app:wrapped_test.py"),
        )
    )

    assert (
        reachability.find_orphans(
            ["app/wrapped_test.py"], ["//app:wrapped_test"], graph
        )
        == ()
    )


def test_non_python_test_and_non_source_references_do_not_mask_orphan():
    graph = reachability.parse_query_graph(
        _xml(
            _rule(
                "//app:real_test",
                rule_class="py_test",
                srcs=("//app:test_support",),
            ),
            _rule(
                "//app:test_support",
                rule_class="py_library",
                srcs=("//app:test_support.py",),
                data=("//app:orphan_test.py",),
            ),
            _rule(
                "//app:documentation",
                rule_class="filegroup",
                srcs=("//app:orphan_test.py",),
            ),
            _rule(
                "//app:orphan_test_semgrep_test",
                rule_class="semgrep_test",
                srcs=("//app:orphan_test.py",),
            ),
            _source("//app:test_support.py"),
            _source("//app:orphan_test.py"),
        )
    )

    assert reachability.find_orphans(
        ["app/orphan_test.py"], ["//app:real_test"], graph
    ) == ("app/orphan_test.py",)


def test_deleting_registration_makes_source_orphaned():
    graph = reachability.parse_query_graph(_xml(_source("//agent:mcp_test.py")))

    assert reachability.find_orphans(["agent/mcp_test.py"], [], graph) == (
        "agent/mcp_test.py",
    )


def test_orphan_diagnostic_names_actionable_repository_path():
    message = reachability.format_orphans(["projects/monolith/agent/mcp_test.py"])

    assert "projects/monolith/agent/mcp_test.py" in message
    assert "Add or restore a test target" in message
    assert "data entry" in message
    assert "scanner target" in message


def test_malformed_query_output_fails_closed():
    with pytest.raises(reachability.ReachabilityError, match="cannot parse"):
        reachability.parse_query_graph("<query>")
