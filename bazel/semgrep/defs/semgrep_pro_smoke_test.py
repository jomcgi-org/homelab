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
