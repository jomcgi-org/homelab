"""Hermetic process tests for the source and manifest scan wrappers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPERS = Path(__file__).parent
FAKE_ENGINE = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with Path(os.environ["ENGINE_CALLS"]).open("a") as stream:
    stream.write(json.dumps([Path(__file__).name, sys.argv[1:]]) + "\n")

if sys.argv[1:] == ["-version"]:
    if Path(__file__).name.endswith("proprietary") and os.environ.get("PROBE_FAIL"):
        print("synthetic loader failure", file=sys.stderr)
        sys.exit(127)
    print("1.168.0")
    sys.exit(0)

if os.environ.get("ENGINE_FAIL"):
    print("synthetic scan failure", file=sys.stderr)
    sys.exit(7)

results = []
if os.environ.get("FINDING"):
    results.append({
        "check_id": "fixture.finding",
        "path": "input.py",
        "start": {"line": 1},
        "extra": {"message": "fixture finding"},
    })
errors = [{"type": "synthetic"}] if os.environ.get("ENGINE_ERRORS") else []
scanned = [] if os.environ.get("ZERO_WORK") else ["input.py"]
print(json.dumps({"results": results, "errors": errors, "paths": {"scanned": scanned}}))
"""
FAKE_UPLOAD = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

record = {
    "args": sys.argv[1:],
    "results": json.loads(Path(sys.argv[1]).read_text()),
    "url": os.environ["SEMGREP_URL"],
    "version": os.environ["SEMGREP_ENGINE_VERSION"],
}
Path(os.environ["UPLOAD_CALL"]).write_text(json.dumps(record))
sys.exit(int(os.environ.get("UPLOAD_EXIT", "0")))
"""


def run_wrapper(tmp_path, kind, *, token="token", **settings):
    runfiles = tmp_path / "runfiles"
    (runfiles / "oss").mkdir(parents=True)
    (runfiles / "pro").mkdir()
    for directory, name in (
        (runfiles / "oss", "semgrep-core"),
        (runfiles / "pro", "semgrep-core-proprietary"),
    ):
        executable = directory / name
        executable.write_text(FAKE_ENGINE)
        executable.chmod(0o755)

    upload = tmp_path / "upload"
    upload.write_text(FAKE_UPLOAD)
    upload.chmod(0o755)
    (tmp_path / "rule.yaml").write_text("rules: []\n")
    (tmp_path / "input.py").write_text("print('fixture')\n")
    test_tmp = tmp_path / "test-tmp"
    test_tmp.mkdir()
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
        "RUNFILES_DIR": str(runfiles),
        "TEST_TMPDIR": str(test_tmp),
        "UPLOAD_SCRIPT": str(upload),
        "UPLOAD_CALL": str(tmp_path / "upload-call.json"),
        "ENGINE_CALLS": str(tmp_path / "engine-calls.jsonl"),
    }
    if token is not None:
        env["SEMGREP_APP_TOKEN"] = token
    env.update({key.upper(): str(value) for key, value in settings.items()})

    if kind == "source":
        script = WRAPPERS / "semgrep-test.sh"
        args = ["rule.yaml", "--", "input.py"]
    else:
        helm = tmp_path / "helm"
        helm.write_text("#!/usr/bin/env bash\nprintf 'kind: Pod\\n'\n")
        helm.chmod(0o755)
        script = WRAPPERS / "semgrep-manifest-test.sh"
        args = [str(helm), "fixture", ".", "default", "rule.yaml", "--"]

    return subprocess.run(
        ["bash", str(script), *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize("kind", ["source", "manifest"])
@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_or_empty_credentials_fail_before_scanning(tmp_path, kind, token):
    result = run_wrapper(tmp_path, kind, token=token)

    assert result.returncode == 2
    assert "SEMGREP_APP_TOKEN is required" in result.stderr
    assert not (tmp_path / "engine-calls.jsonl").exists()
    assert not (tmp_path / "upload-call.json").exists()
    assert "SKIPPED" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["source", "manifest"])
@pytest.mark.parametrize(("finding", "expected_exit"), [(False, 0), (True, 1)])
def test_clean_and_findings_scans_upload_the_local_outcome(
    tmp_path, kind, finding, expected_exit
):
    result = run_wrapper(tmp_path, kind, finding="1" if finding else "")

    assert result.returncode == expected_exit, result.stdout + result.stderr
    uploaded = json.loads((tmp_path / "upload-call.json").read_text())
    assert uploaded["args"][1] == str(expected_exit)
    assert len(uploaded["results"]["results"]) == expected_exit
    assert uploaded["url"] == "https://semgrep.dev"
    assert uploaded["version"] == "1.168.0"
    assert "SKIPPED" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["source", "manifest"])
@pytest.mark.parametrize(
    "failure", ["probe_fail", "engine_fail", "engine_errors", "zero_work"]
)
def test_engine_failures_cannot_become_clean_or_upload(tmp_path, kind, failure):
    result = run_wrapper(tmp_path, kind, **{failure: "1"})
    diagnostic = result.stdout + result.stderr

    assert result.returncode == 2, diagnostic
    assert "PASSED:" not in diagnostic
    assert "SKIPPED" not in diagnostic
    assert not (tmp_path / "upload-call.json").exists()


@pytest.mark.parametrize("kind", ["source", "manifest"])
@pytest.mark.parametrize(("finding", "expected_exit"), [(False, 0), (True, 1)])
def test_upload_process_failure_preserves_local_scan_exit(
    tmp_path, kind, finding, expected_exit
):
    result = run_wrapper(
        tmp_path,
        kind,
        finding="1" if finding else "",
        upload_exit="9",
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert (tmp_path / "upload-call.json").exists()
    assert "helper failed (non-fatal)" in result.stderr
