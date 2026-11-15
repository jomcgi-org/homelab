"""Print the canonical before/after Helm render diff for the HOME-69 PR body.

Run from the repository root with HELM_BIN set. Requires git history containing
the migration base; this is a review helper, not a hermetic Bazel test.
"""

import difflib
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

BASE = "e68a9f09b7da28ed39254c7129b70399890b1955"
CHART = Path("projects/factory-landing/chart")
DIGEST = "sha256:" + "0" * 64


def render(chart):
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-landing",
            str(chart),
            "--namespace",
            "factory-landing",
            "--set",
            f"image.digest={DIGEST}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    docs.sort(key=lambda doc: (doc["kind"], doc["metadata"]["name"]))
    return yaml.safe_dump_all(docs, sort_keys=True, width=120).splitlines(keepends=True)


def main():
    with tempfile.TemporaryDirectory() as directory:
        before = Path(directory)
        for name in (
            "Chart.yaml",
            "values.yaml",
            "templates/landing.yaml",
            "templates/image-pull-secret.yaml",
        ):
            source = subprocess.run(
                ["git", "show", f"{BASE}:{CHART / name}"],
                check=True,
                capture_output=True,
            ).stdout
            path = before / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source)
        print(
            "".join(
                difflib.unified_diff(
                    render(before), render(CHART), fromfile="before", tofile="after"
                )
            ),
            end="",
        )


if __name__ == "__main__":
    main()
