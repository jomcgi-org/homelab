from pathlib import Path

import pytest
from verification_genrule_guard import GuardError, main, scan_repository

FAILING_BUILD = """
genrule(
    name = "tlc_missing_tag",
    outs = ["tlc_missing_tag.txt"],
    cmd = "touch $@",
)

genrule(
    name = "api_smoke",
    outs = ["api_smoke.txt"],
    cmd = "touch $@",
    tags = [],
)

genrule(
    name = "migration_test",
    outs = ["migration_test.txt"],
    cmd = "touch $@",
    tags = ["manual"],
)

genrule(
    tags = ["local"],
    cmd = "touch $@",
    name = "attribute_order_test",
    outs = ["attribute_order_test.txt"],
)

genrule(
    name = "tagged_smoke",
    outs = ["tagged_smoke.txt"],
    cmd = "touch $@",
    tags = ["manual", "verification"],
)

genrule(
    name = "asset_bundle",
    outs = ["asset_bundle.txt"],
    cmd = "touch $@",
)
"""

PASSING_BUILD = """
genrule(
    name = "tlc_missing_tag",
    outs = ["tlc_missing_tag.txt"],
    cmd = "touch $@",
    tags = ["verification"],
)

genrule(
    name = "api_smoke",
    outs = ["api_smoke.txt"],
    cmd = "touch $@",
    tags = ["verification"],
)

genrule(
    name = "migration_test",
    outs = ["migration_test.txt"],
    cmd = "touch $@",
    tags = ["manual", "verification"],
)

genrule(
    tags = ["local", "verification"],
    cmd = "touch $@",
    name = "attribute_order_test",
    outs = ["attribute_order_test.txt"],
)

genrule(
    name = "tagged_smoke",
    outs = ["tagged_smoke.txt"],
    cmd = "touch $@",
    tags = ["manual", "verification"],
)

genrule(
    name = "asset_bundle",
    outs = ["asset_bundle.txt"],
    cmd = "touch $@",
)
"""

FAILING_NATIVE_BUILD = """
native.genrule(
    name = "native_suite_test",
    outs = ["native_suite_test.txt"],
    cmd = "touch $@",
)

native.genrule(
    name = "native_tagged_test",
    outs = ["native_tagged_test.txt"],
    cmd = "touch $@",
    tags = ["verification"],
)
"""

PASSING_NATIVE_BUILD = """
native.genrule(
    name = "native_suite_test",
    outs = ["native_suite_test.txt"],
    cmd = "touch $@",
    tags = ["verification"],
)

native.genrule(
    name = "native_tagged_test",
    outs = ["native_tagged_test.txt"],
    cmd = "touch $@",
    tags = ["verification"],
)
"""


def _write(root: Path, relative: str, content: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def test_negative_and_corrected_fixtures_cover_new_package_and_build_forms(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "BUILD", FAILING_BUILD)
    _write(tmp_path, "new-package/BUILD.bazel", FAILING_NATIVE_BUILD)
    _write(
        tmp_path,
        "ignored.bzl",
        'genrule(name = "ignored_test", tags = [], outs = ["x"], cmd = "touch $@")',
    )

    result = scan_repository(tmp_path)
    assert result.files_scanned == 2
    assert [finding.name for finding in result.findings] == [
        "tlc_missing_tag",
        "api_smoke",
        "migration_test",
        "attribute_order_test",
        "native_suite_test",
    ]
    assert result.findings[-1].kind == "native.genrule"
    assert result.findings[-1].path == Path("new-package/BUILD.bazel")

    assert main([str(tmp_path)]) == 1
    failing_output = capsys.readouterr()
    assert "FAILED: scanned 2 BUILD file(s), found 5" in failing_output.err
    assert "ignored_test" not in failing_output.err

    _write(tmp_path, "BUILD", PASSING_BUILD)
    _write(tmp_path, "new-package/BUILD.bazel", PASSING_NATIVE_BUILD)

    assert main([str(tmp_path)]) == 0
    passing_output = capsys.readouterr()
    assert passing_output.err == ""
    assert (
        "PASSED: scanned 2 BUILD file(s), no missing verification tags"
        in passing_output.out
    )


def test_parse_failure_is_not_reported_as_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, "BUILD", 'genrule(name = "broken_test"')

    with pytest.raises(GuardError, match="could not parse BUILD file"):
        scan_repository(tmp_path)
    assert main([str(tmp_path)]) == 2
    assert "could not parse BUILD file" in capsys.readouterr().err


def test_empty_tree_is_not_reported_as_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(GuardError, match="no BUILD or BUILD.bazel files found"):
        scan_repository(tmp_path)
    assert main([str(tmp_path)]) == 2
    assert "no BUILD or BUILD.bazel files found" in capsys.readouterr().err
