"""Linux pinned-engine controls: a finding exit must be a real known finding."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("kind", ["source", "manifest"])
@pytest.mark.parametrize("positive", [True, False], ids=["positive", "clean"])
def test_real_pro_wrapper_control(tmp_path, kind, positive):
    repo = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    wrappers = repo / "bazel/semgrep/defs"
    runfiles = Path(os.environ["TEST_SRCDIR"])
    helm = Path(os.environ["SEMGREP_SMOKE_HELM"]).absolute()
    work = tmp_path / "fixture"
    work.mkdir()
    test_tmp = tmp_path / "scan-state"
    test_tmp.mkdir()
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
        "RUNFILES_DIR": str(runfiles),
        "TEST_WORKSPACE": os.environ["TEST_WORKSPACE"],
        "TEST_TMPDIR": str(test_tmp),
        "SEMGREP_APP_TOKEN": "semgrep-wrapper-offline-smoke",
        "SEMGREP_URL": "http://127.0.0.1:0",
    }
    if kind == "source":
        rule_id = "no-shell-true"
        shutil.copyfile(
            repo / f"bazel/semgrep/rules/python/{rule_id}.yaml", work / "rule.yaml"
        )
        statement = (
            'subprocess.run("offline fixture", shell=True)'
            if positive
            else 'subprocess.run(["offline", "fixture"])'
        )
        (work / "input.py").write_text("import subprocess\n" + statement + "\n")
        script = "semgrep-test.sh"
        args = ["rule.yaml", "--", "input.py"]
        expected_path = "input.py"
        expected_line = 2
    else:
        rule_id = "no-privileged"
        shutil.copyfile(
            repo / f"bazel/semgrep/rules/kubernetes/{rule_id}.yaml", work / "rule.yaml"
        )
        (work / "chart/templates").mkdir(parents=True)
        (work / "chart/Chart.yaml").write_text(
            "apiVersion: v2\nname: smoke\nversion: 0.0.0\n"
        )
        (work / "chart/templates/pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: smoke\nspec:\n"
            "  containers:\n    - name: smoke\n      image: example.invalid/offline\n"
            "      securityContext:\n        privileged: "
            + ("true" if positive else "false")
            + "\n"
        )
        script = "semgrep-manifest-test.sh"
        args = [str(helm), "smoke", "chart", "default", "rule.yaml", "--"]
        expected_path = "rendered-manifests.yaml"
        expected_line = (
            11  # Helm's document separator and source comment add two lines.
        )
    # Bazel sh_test copies/renames the script into the consuming package. Run
    # the real Pro controls from that layout, with support only in runfiles.
    copied_wrapper = work / "consumer_package/scanner_test"
    copied_wrapper.parent.mkdir()
    shutil.copyfile(wrappers / script, copied_wrapper)
    result = subprocess.run(
        ["bash", str(copied_wrapper), *args],
        cwd=work,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    diagnostic = result.stdout + result.stderr
    # Preserve concrete scan evidence in the successful Linux test log too.
    print(
        f"REAL PRO CONTROL: kind={kind} positive={positive} exit={result.returncode}\n{diagnostic}"
    )
    assert result.returncode == (1 if positive else 0), diagnostic
    assert "INFRASTRUCTURE:" not in diagnostic
    assert "ENGINE: semgrep-core version=" in diagnostic
    assert "ENGINE: semgrep-core-proprietary version=" in diagnostic
    merged = json.loads((test_tmp / "results.json").read_text())
    assert merged["paths"]["scanned"] == [expected_path], merged
    assert merged["errors"] == []
    actual = {
        (match["check_id"], match["path"], match["start"]["line"])
        for match in merged["results"]
    }
    assert actual == (
        {(rule_id, expected_path, expected_line)} if positive else set()
    ), merged
    assert len(merged["results"]) == int(positive), merged


@pytest.mark.parametrize("positive", [True, False], ids=["positive", "clean"])
def test_real_pro_lockfile_only_control(tmp_path, positive):
    repo = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    wrappers = repo / "bazel/semgrep/defs"
    work = tmp_path / "fixture"
    work.mkdir()
    test_tmp = tmp_path / "scan-state"
    test_tmp.mkdir()
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
        "RUNFILES_DIR": os.environ["TEST_SRCDIR"],
        "TEST_WORKSPACE": os.environ["TEST_WORKSPACE"],
        "TEST_TMPDIR": str(test_tmp),
        "SEMGREP_APP_TOKEN": "semgrep-wrapper-offline-smoke",
        "SEMGREP_URL": "http://127.0.0.1:0",
    }
    rule_id = "ssc-00000000-0000-4000-8000-000000000001"
    (work / "rule.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": rule_id,
                        "languages": ["python"],
                        "severity": "WARNING",
                        "message": "Offline lockfile wrapper control",
                        "metadata": {"sca-kind": "legacy"},
                        "r2c-internal-project-depends-on": {
                            "depends-on-either": [
                                {
                                    "namespace": "pypi",
                                    "package": "semgrep-offline-fixture",
                                    "version": "< 2.0.0",
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )
    lockfile = work / "requirements.txt"
    lockfile.write_text(
        "semgrep-offline-fixture==" + ("1.0.0" if positive else "2.0.0") + "\n"
    )
    copied_wrapper = work / "consumer_package/scanner_test"
    copied_wrapper.parent.mkdir()
    shutil.copyfile(wrappers / "semgrep-test.sh", copied_wrapper)
    result = subprocess.run(
        # Keep the empty source section. A dummy source would hide a broken
        # DependencySourceTarget path in the production wrapper.
        ["bash", str(copied_wrapper), "rule.json", "--", "--", "requirements.txt"],
        cwd=work,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    diagnostic = result.stdout + result.stderr
    print(
        f"REAL PRO LOCKFILE CONTROL: positive={positive} exit={result.returncode}\n{diagnostic}"
    )
    assert result.returncode == (1 if positive else 0), diagnostic
    assert "INFRASTRUCTURE:" not in diagnostic

    assert "PASS START: index=0 SCA" in diagnostic
    assert "SAST" not in diagnostic
    staged_lockfile = test_tmp / "scan/requirements.txt"
    controlled_paths = {lockfile.resolve(), staged_lockfile.resolve()}

    def is_controlled_lockfile(path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = work / candidate
        return candidate.resolve() in controlled_paths

    # Core may report the original or staged lockfile. Accept only these
    # exact identities, never another file sharing the same basename.
    raw = json.loads((test_tmp / "results/result_0.json").read_text())
    assert raw["paths"]["scanned"], raw
    assert all(is_controlled_lockfile(path) for path in raw["paths"]["scanned"]), raw
    assert raw["errors"] == [], raw
    if positive:
        assert len(raw["results"]) == 1, raw
        match = raw["results"][0]
        assert match["check_id"] == rule_id, raw
        assert is_controlled_lockfile(match["path"]), raw
        assert match["start"]["line"] == 1, raw

    merged = json.loads((test_tmp / "results.json").read_text())
    assert merged["paths"]["scanned"], merged
    assert all(is_controlled_lockfile(path) for path in merged["paths"]["scanned"]), (
        merged
    )
    assert len(merged["results"]) == int(positive), merged
    if positive:
        match = merged["results"][0]
        assert match["check_id"] == rule_id, merged
        assert is_controlled_lockfile(match["path"]), merged
        assert match["start"]["line"] == 1, merged
    # A clean raw finding may be removed by the existing SCA version filter.
    # This control proves the complete wrapper, not native core version checks.
