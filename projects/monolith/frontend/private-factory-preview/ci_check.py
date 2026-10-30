"""Invoked by the existing secret-free Factory mobile preview entry point."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    frontend = Path(__file__).resolve().parent.parent
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True
    ).strip()
    assert actual == args.expected_sha, (
        "private fixture checkout does not match requested SHA"
    )
    commands = [
        [
            "pnpm",
            "exec",
            "vitest",
            "run",
            "src/routes/private/factory/page.test.js",
            "src/routes/private/factory/factory-view.test.js",
            "src/routes/private/factory/escalations",
            "src/routes/private/factory/work-items/[id]",
        ],
        [
            "pnpm",
            "exec",
            "vitest",
            "run",
            "--config",
            "private-factory-preview/vitest.config.js",
        ],
    ]
    for command in commands:
        subprocess.run(command, cwd=frontend, check=True)
    with tempfile.TemporaryDirectory(prefix="private-factory-fixture-") as artifact:
        subprocess.run(
            [
                "pnpm",
                "exec",
                "vite",
                "build",
                "--config",
                "private-factory-preview/vite.config.js",
            ],
            cwd=frontend,
            env={
                **os.environ,
                "FACTORY_PREVIEW_SHA": args.expected_sha,
                "PRIVATE_FACTORY_PREVIEW_OUT": artifact,
            },
            check=True,
        )

        subprocess.run(
            [
                sys.executable,
                str(frontend / "private-factory-preview/dashboard_browser_check.py"),
                "--artifact",
                artifact,
                "--output",
                str(args.output.resolve() / "dashboard"),
                "--expected-sha",
                args.expected_sha,
            ],
            cwd=frontend,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(frontend / "private-factory-preview/browser_check.py"),
                "--artifact",
                artifact,
                "--output",
                str(args.output.resolve()),
                "--expected-sha",
                args.expected_sha,
            ],
            cwd=frontend,
            check=True,
        )


if __name__ == "__main__":
    main()
