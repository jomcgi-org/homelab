"""Hermetic process-level regressions for both production shell wrappers."""

import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest


WRAPPERS = (
    Path(os.environ["TEST_SRCDIR"])
    / os.environ["TEST_WORKSPACE"]
    / "bazel/semgrep/defs"
    if "TEST_SRCDIR" in os.environ
    else Path(__file__).parent
)
FAKE_ENGINE = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

here = Path(__file__).parent
plan = json.loads(Path(os.environ["FAKE_PLAN"]).read_text())
pro = Path(__file__).name.endswith("proprietary")
with Path(os.environ["FAKE_CALLS"]).open("a") as log:
    log.write(json.dumps([Path(__file__).name, sys.argv[1:]]) + "\\n")
if sys.argv[1:] == ["-version"]:
    # These only exist after both executables and both sets of libs are staged.
    assert (here / "semgrep-core").exists()
    assert (here / "semgrep-core-proprietary").exists()
    assert (here / "libs" / "oss-lib").exists()
    assert (here / "libs" / "pro-lib").exists()
    probe = plan.get("pro_probe" if pro else "oss_probe", {})
    print(probe.get("version", "1.168.0"))
    sys.exit(probe.get("exit", 0))
assert pro, "the real scan must use Pro"
state = Path(os.environ["FAKE_STATE"])
index = int(state.read_text()) if state.exists() else 0
state.write_text(str(index + 1))
response = plan["passes"][index]
if "raw" in response:
    print(response["raw"])
else:
    print(json.dumps(response["output"]))
print("synthetic engine diagnostic", file=sys.stderr)
sys.exit(response.get("exit", 0))
"""


def output(*, finding=False, scanned=None):
    result = {
        "version": "1.168.0",
        "results": [],
        "errors": [],
        "paths": {"scanned": ["input.py"] if scanned is None else scanned},
    }
    if finding:
        result["results"] = [
            {
                "check_id": "test.finding",
                "path": "input.py",
                "start": {"line": 2, "col": 1, "offset": 10},
                "end": {"line": 2, "col": 4, "offset": 13},
                "extra": {"metavars": {}, "engine_kind": "PRO", "is_ignored": False},
            }
        ]
    return result


@pytest.fixture(params=["source", "manifest"])
def wrapper(request):
    return request.param


def run_wrapper(
    tmp_path,
    wrapper,
    responses=None,
    *,
    plan_extra=None,
    env_extra=None,
    rules=("rule.yaml",),
    rule_contents=None,
    source="input.py",
    lockfile=None,
    helm="render",
    copied_wrapper=False,
    missing_support=False,
    runfiles_env="RUNFILES_DIR",
):
    runfiles = tmp_path / "runfiles"
    for kind, name in (("oss", "semgrep-core"), ("pro", "semgrep-core-proprietary")):
        directory = runfiles / kind
        (directory / "libs").mkdir(parents=True)
        (directory / "libs" / f"{kind}-lib").write_text("test dependency")
        executable = directory / name
        executable.write_text(FAKE_ENGINE)
        executable.chmod(0o755)
    plan = {"passes": responses if responses is not None else [{"output": output()}]}
    plan.update(plan_extra or {})
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    for rule in rules:
        (tmp_path / rule).write_text(json.dumps(rule_contents or {"rules": []}))
    if source:
        (tmp_path / source).write_text("first line\nsecond line\n")
    if lockfile:
        (tmp_path / lockfile).write_text("fixture-package==2.0.0\n")
    test_tmp = tmp_path / "test-tmp"
    test_tmp.mkdir()
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
        runfiles_env: str(runfiles),
        "TEST_TMPDIR": str(test_tmp),
        "SEMGREP_APP_TOKEN": "fake-engine-offline-test",
        "SEMGREP_URL": "http://127.0.0.1:0",
        "FAKE_PLAN": str(tmp_path / "plan.json"),
        "FAKE_CALLS": str(tmp_path / "calls.jsonl"),
        "FAKE_STATE": str(tmp_path / "state"),
    }
    env.update(env_extra or {})
    if wrapper == "source":
        args = [*rules, "--", *([source] if source else [])]
        if lockfile:
            args += ["--", lockfile]
        script = "semgrep-test.sh"
    else:
        fake_helm = tmp_path / "helm"
        render = {
            "render": "printf 'kind: Pod\\n'",
            "empty": ":",
            "whitespace": "printf ' \\n'",
            "fail": "exit 9",
        }[helm]
        fake_helm.write_text("#!/usr/bin/env bash\n" + render + "\n")
        fake_helm.chmod(0o755)
        args = [str(fake_helm), "fixture", ".", "default", *rules, "--"]
        script = "semgrep-manifest-test.sh"
    executable = WRAPPERS / script
    if copied_wrapper:
        # Model rules_shell's renamed copy in a different consumer package.
        executable = runfiles / "_main/consumer_package/scanner_test"
        executable.parent.mkdir(parents=True)
        shutil.copyfile(WRAPPERS / script, executable)
        if not missing_support:
            support = runfiles / "_main/bazel/semgrep/defs"
            support.mkdir(parents=True)
            for filename in ("semgrep-common.sh", "semgrep-output.py"):
                shutil.copyfile(WRAPPERS / filename, support / filename)
    result = subprocess.run(
        ["bash", str(executable), *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    return result


def assert_infrastructure(result):
    assert result.returncode == 2, result.stdout + result.stderr
    assert "INFRASTRUCTURE:" in result.stderr + result.stdout
    assert "PASSED:" not in result.stdout
    assert "FAILED: Semgrep found violations" not in result.stdout


def test_staged_oss_and_pro_probes_then_validated_clean_scan(tmp_path, wrapper):
    result = run_wrapper(tmp_path, wrapper)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert calls[0] == ["semgrep-core", ["-version"]]
    assert calls[1] == ["semgrep-core-proprietary", ["-version"]]
    assert calls[2][0] == "semgrep-core-proprietary"
    assert "SCANNED: 1 distinct engine-confirmed path(s)" in result.stdout
    assert "PASS START: index=0" in result.stdout
    assert "PASS END: index=0 exit=0" in result.stdout


@pytest.mark.parametrize("probe", ["oss_probe", "pro_probe"])
@pytest.mark.parametrize("failure", [{"exit": 127}, {"version": ""}])
def test_probe_failure_is_infrastructure(tmp_path, wrapper, probe, failure):
    result = run_wrapper(tmp_path, wrapper, plan_extra={probe: failure})
    assert_infrastructure(result)
    assert "PASS START" not in result.stdout


@pytest.mark.parametrize("finding", [False, True])
def test_nonzero_exit_cannot_be_cleared_by_valid_json_or_exclusion(
    tmp_path, wrapper, finding
):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"exit": 127, "output": output(finding=finding)}],
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert_infrastructure(result)
    assert "engine exit=127" in result.stderr
    assert "synthetic engine diagnostic" in result.stderr


def bad_outputs():
    invalid = [{"raw": value} for value in ("", "{", "null", "[]", "{}")]
    for field in ("version", "results", "errors", "paths"):
        data = output()
        del data[field]
        invalid.append({"output": data})
    for value in (None, {}, "", 1):
        data = output()
        data["results"] = value
        invalid.append({"output": data})
    for paths in ({}, {"scanned": None}, {"scanned": [3]}):
        data = output()
        data["paths"] = paths
        invalid.append({"output": data})
    for key, value in (
        ("errors", [{"error_type": "Timeout"}]),
        ("errors", {}),
        ("skipped_rules", {}),
        ("skipped_rules", [{"rule_id": "bad"}]),
    ):
        data = output()
        data[key] = value
        invalid.append({"output": data})
    for field in ("check_id", "path", "start", "end", "extra"):
        data = output(finding=True)
        del data["results"][0][field]
        invalid.append({"output": data})
    for field in ("metavars", "engine_kind", "is_ignored"):
        data = output(finding=True)
        del data["results"][0]["extra"][field]
        invalid.append({"output": data})
    return invalid


@pytest.mark.parametrize("response", bad_outputs())
def test_incomplete_or_invalid_output_never_passes(tmp_path, wrapper, response):
    assert_infrastructure(
        run_wrapper(
            tmp_path,
            wrapper,
            [response],
            env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
        )
    )


def test_later_failed_pass_cannot_be_hidden_by_first_success(tmp_path, wrapper):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": output()}, {"raw": "{"}],
        rules=("one.yaml", "two.yaml"),
    )
    assert_infrastructure(result)
    assert "PASS END: index=0 exit=0" in result.stdout
    assert "PASS START: index=1" in result.stdout


def test_empty_rule_language_intersection_is_allowed_with_other_engine_work(
    tmp_path, wrapper
):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": output(scanned=[])}, {"output": output()}],
        rules=("one.yaml", "two.yaml"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCANNED: 1 distinct engine-confirmed path(s), 2 pass(es)" in result.stdout


def test_aggregate_zero_work_fails_even_with_findings_excluded(tmp_path, wrapper):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": output(finding=True, scanned=[])}],
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert_infrastructure(result)
    assert "zero engine-confirmed" in result.stderr


def test_scanned_paths_deduplicated_across_passes(tmp_path, wrapper):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": output()}, {"output": output()}],
        rules=("one.yaml", "two.yaml"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCANNED: 1 distinct engine-confirmed path(s), 2 pass(es)" in result.stdout


def test_exact_finding_status_and_location(tmp_path, wrapper):
    data = output(finding=True)
    # The pinned protocol permits absent offsets and a tagged Pro feature.
    del data["results"][0]["start"]["offset"]
    data["results"][0]["extra"]["engine_kind"] = [
        "PRO_REQUIRED",
        {
            "interproc_taint": True,
            "interfile_taint": False,
            "proprietary_language": False,
        },
    ]
    result = run_wrapper(tmp_path, wrapper, [{"output": data}])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test.finding at input.py:2" in result.stdout
    assert "FAILED: Semgrep found violations (1 finding(s))" in result.stdout
    assert "INFRASTRUCTURE:" not in result.stderr


def test_valid_finding_exclusion_preserves_work_evidence(tmp_path, wrapper):
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": output(finding=True)}],
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads((tmp_path / "test-tmp" / "results.json").read_text())
    assert merged["results"] == []
    assert merged["paths"]["scanned"] == ["input.py"]


def test_explicit_all_rules_excluded_is_identified_as_policy(tmp_path, wrapper):
    result = run_wrapper(tmp_path, wrapper, env_extra={"SEMGREP_EXCLUDE_RULES": "rule"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "POLICY: All 1 rule files explicitly excluded" in result.stdout
    assert "PASSED:" not in result.stdout


def test_annotation_mode_fails_even_when_all_rules_excluded(tmp_path, wrapper):
    result = run_wrapper(
        tmp_path,
        wrapper,
        env_extra={
            "SEMGREP_TEST_MODE": "1",
            "SEMGREP_EXCLUDE_RULES": "rule",
        },
    )
    assert_infrastructure(result)
    assert "annotation comparison is not implemented" in result.stderr


@pytest.mark.parametrize("helm", ["fail", "empty", "whitespace"])
def test_bad_helm_render_is_infrastructure(tmp_path, helm):
    assert_infrastructure(run_wrapper(tmp_path, "manifest", helm=helm))


def test_unrecognized_source_extension_cannot_report_clean(tmp_path):
    assert_infrastructure(run_wrapper(tmp_path, "source", source="input.unknown"))


def test_missing_rules_are_not_an_explicit_exclusion(tmp_path, wrapper):
    assert_infrastructure(run_wrapper(tmp_path, wrapper, rules=()))


def test_lockfile_only_sca_keeps_engine_reported_work(tmp_path):
    result = run_wrapper(
        tmp_path,
        "source",
        [{"output": output(scanned=["requirements.txt"])}],
        source=None,
        lockfile="requirements.txt",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    targets = json.loads((tmp_path / "test-tmp" / "sca_targets.json").read_text())
    assert targets[0] == "Targets"
    assert targets[1][0][0] == "DependencySourceTarget"
    assert targets[1][0][1][0] == "LockfileOnly"
    assert "SCA rule=" in result.stdout and "rule.yaml" in result.stdout
    assert "SAST" not in result.stdout


def test_sca_execution_failure_cannot_be_filtered(tmp_path):
    result = run_wrapper(
        tmp_path,
        "source",
        [{"exit": 127, "output": output(finding=True)}],
        source=None,
        lockfile="requirements.txt",
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert_infrastructure(result)


@pytest.mark.parametrize("constraint, expected_exit", [("<2.0.0", 0), ("<=2.0.0", 1)])
def test_lockfile_version_filter_only_changes_valid_findings(
    tmp_path, constraint, expected_exit
):
    data = output(finding=True, scanned=["requirements.txt"])
    data["results"][0]["check_id"] = "ssc-123abc"
    data["results"][0]["path"] = "requirements.txt"
    rule = {
        "rules": [
            {
                "id": "ssc-123abc",
                "r2c-internal-project-depends-on": {
                    "depends-on-either": [
                        {"package": "fixture-package", "version": constraint},
                    ]
                },
            }
        ]
    }
    result = run_wrapper(
        tmp_path,
        "source",
        [{"output": data}],
        source=None,
        lockfile="requirements.txt",
        rule_contents=rule,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    merged = json.loads((tmp_path / "test-tmp" / "results.json").read_text())
    assert len(merged["results"]) == expected_exit
    assert merged["paths"]["scanned"] == ["requirements.txt"]


@pytest.mark.parametrize("runfiles_env", ["RUNFILES_DIR", "TEST_SRCDIR"])
def test_copied_wrapper_resolves_declared_runfiles_support(
    tmp_path, wrapper, runfiles_env
):
    result = run_wrapper(
        tmp_path, wrapper, copied_wrapper=True, runfiles_env=runfiles_env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCANNED: 1 distinct engine-confirmed path(s)" in result.stdout


def test_copied_wrapper_missing_support_fails_as_infrastructure(tmp_path, wrapper):
    result = run_wrapper(tmp_path, wrapper, copied_wrapper=True, missing_support=True)
    assert_infrastructure(result)
    assert "wrapper support not found in runfiles" in result.stderr


@pytest.mark.parametrize(
    "metavar",
    [
        None,
        {},
        {"start": {"line": 2, "col": 1}, "end": {"line": 2, "col": 1}},
        {"start": None, "end": {"line": 2, "col": 1}, "abstract_content": ""},
        {
            "start": {"line": True, "col": 1},
            "end": {"line": 2, "col": 1},
            "abstract_content": "",
        },
        {
            "start": {"line": 2, "col": 1},
            "end": {"line": 2, "col": 1},
            "abstract_content": None,
        },
        {
            "start": {"line": 2, "col": 1},
            "end": {"line": 2, "col": 1},
            "abstract_content": "",
            "propagated_value": {},
        },
    ],
)
def test_invalid_metavar_cannot_be_excluded_to_pass(tmp_path, wrapper, metavar):
    data = output(finding=True)
    data["results"][0]["extra"]["metavars"] = {"$X": metavar}
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": data}],
        copied_wrapper=True,
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert_infrastructure(result)
    assert "metavar" in result.stderr


def test_valid_empty_metavar_and_propagated_value_can_be_excluded(tmp_path, wrapper):
    data = output(finding=True)
    data["results"][0]["extra"]["metavars"] = {
        "$...ARGS": {
            "start": {"line": 2, "col": 1},
            "end": {"line": 2, "col": 1},
            "abstract_content": "",
            "propagated_value": {"svalue_abstract_content": ""},
        }
    }
    result = run_wrapper(
        tmp_path,
        wrapper,
        [{"output": data}],
        copied_wrapper=True,
        env_extra={"SEMGREP_EXCLUDE_RULES": "finding"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXCLUDED: 1 finding(s)" in result.stdout
