"""Run full-layout comparisons inside the existing Linux preview job.

This is test orchestration, not a fixture endpoint. Only public source history
and a pinned test dependency are downloaded. Rendered pages remain offline.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run(output, expected_sha, *, capture_only=False):
    frontend = Path(__file__).resolve().parent.parent
    repo = frontend.parents[2]
    assert re.fullmatch(r"[0-9a-f]{40}", expected_sha), expected_sha

    def command(args, *, cwd=frontend, env=None):
        print("sheet QA:", " ".join(map(str, args)), flush=True)
        subprocess.run(args, cwd=cwd, env=env, check=True)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    assert git("rev-parse", "HEAD") == expected_sha, (
        "Preview checkout is not the PR head"
    )
    assert not git("status", "--porcelain", "--untracked-files=normal"), (
        "dirty checkout cannot claim exact SHA evidence"
    )
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    target = event["pull_request"]["base"]["sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", target), target
    # The existing job checks out one shallow head. Fetch only the two exact
    # public histories needed to find their common source, never another PR.
    if git("rev-parse", "--is-shallow-repository") == "true":
        command(
            [
                "git",
                "fetch",
                "--no-tags",
                "--unshallow",
                "origin",
                expected_sha,
                target,
            ],
            cwd=repo,
        )
    else:
        command(["git", "fetch", "--no-tags", "origin", target], cwd=repo)
    base = git("merge-base", expected_sha, target)
    if importlib.util.find_spec("PIL") is None:
        command([sys.executable, "-m", "pip", "install", "Pillow==13.0.0"])
    command(
        [
            "pnpm",
            "exec",
            "vitest",
            "run",
            "--config",
            "factory-preview/sheet-vitest.config.js",
        ]
    )
    command(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "factory-preview",
            "-p",
            "sheet_check_test.py",
        ],
        env={**os.environ, "SHEET_REAL_BROWSER": "1"},
    )
    env = {**os.environ, "FACTORY_PREVIEW_SHA": expected_sha}
    command(
        [
            "pnpm",
            "exec",
            "vite",
            "build",
            "--config",
            "factory-preview/sheet-vite.config.js",
        ],
        env=env,
    )
    command(
        [
            sys.executable,
            "factory-preview/sheet_check.py",
            "--artifact",
            "sheet-fixture-preview",
            "--output",
            str(output / "after"),
            "--label",
            expected_sha,
            *([] if capture_only else ["--acceptance"]),
        ]
    )
    with tempfile.TemporaryDirectory(
        prefix="sheet-baseline-", dir=os.environ.get("RUNNER_TEMP")
    ) as temporary:
        baseline = Path(temporary)
        archive = subprocess.check_output(
            ["git", "archive", base, "projects/monolith/frontend/src"], cwd=repo
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(baseline, filter="data")
        baseline_frontend = baseline / "projects/monolith/frontend"
        (baseline_frontend / "node_modules").symlink_to(
            frontend / "node_modules", target_is_directory=True
        )
        artifact = baseline / "artifact"
        command(
            [
                "pnpm",
                "exec",
                "vite",
                "build",
                "--config",
                "factory-preview/sheet-vite.config.js",
            ],
            env={
                **env,
                "SHEET_SOURCE_ROOT": str(baseline_frontend / "src"),
                "SHEET_SOURCE_REVISION": base,
                "SHEET_OUT_DIR": str(artifact),
            },
        )
        command(
            [
                sys.executable,
                "factory-preview/sheet_check.py",
                "--artifact",
                str(artifact),
                "--output",
                str(output / "before"),
                "--label",
                base,
            ]
        )
    (output / "provenance.json").write_text(
        json.dumps(
            {
                "base_sha": base,
                "target_sha": target,
                "head_sha": expected_sha,
                "harness_sha": expected_sha,
                "role": "capture-only" if capture_only else "comparison",
                "visual_acceptance": "independent review required",
            },
            indent=2,
        )
        + "\n"
    )
    if capture_only:
        return
    command(
        [
            sys.executable,
            "factory-preview/sheet_check.py",
            "--before",
            str(output / "before"),
            "--after",
            str(output / "after"),
            "--output",
            str(output / "comparison"),
            "--acceptance",
        ]
    )
