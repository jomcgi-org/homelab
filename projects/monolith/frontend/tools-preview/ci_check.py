"""Use the existing secret-free workflow and its evidence upload directory."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    frontend = Path(__file__).resolve().parent.parent
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True
    ).strip()
    assert actual == args.expected_sha, (
        "Tools fixture checkout is not the requested head"
    )
    subprocess.run(
        ["pnpm", "exec", "vitest", "run", "--config", "tools-preview/vitest.config.js"],
        cwd=frontend,
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="tools-fixture-") as artifact:
        subprocess.run(
            [
                "pnpm",
                "exec",
                "vite",
                "build",
                "--config",
                "tools-preview/vite.config.js",
            ],
            cwd=frontend,
            env={
                **os.environ,
                "FACTORY_PREVIEW_SHA": actual,
                "TOOLS_PREVIEW_OUT": artifact,
            },
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(frontend / "tools-preview/browser_check.py"),
                "--artifact",
                artifact,
                "--output",
                str(args.output.resolve()),
                "--expected-sha",
                actual,
            ],
            cwd=frontend,
            check=True,
        )


if __name__ == "__main__":
    main()
