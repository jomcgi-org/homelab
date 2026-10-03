"""Reproduce full-size local calibration with the real verifier, no model API."""

import argparse
import json
import platform
import tempfile
import time
from pathlib import Path

import yaml
from bench.verifiers.speedup import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 3:
        parser.error("calibration requires at least three runs per implementation")
    task_dir = Path(__file__).resolve().parent.parent
    task = yaml.safe_load((task_dir / "task.yaml").read_text())
    print(
        json.dumps(
            {
                "machine": platform.platform(),
                "python": platform.python_version(),
                "fixture_version": task["verifier"]["args"]["fixture_version"],
                "dataset_parks": task["verifier"]["args"]["harness_args"][0],
            }
        ),
        flush=True,
    )
    for label in ("baseline", "micro", "algorithmic"):
        source = (
            task["snapshot"]["files"]["rollup.py"]
            if label == "baseline"
            else (task_dir / "reference" / (label + ".py")).read_text()
        )
        for run in range(1, args.runs + 1):
            with tempfile.TemporaryDirectory(prefix="rollup-calibration-") as directory:
                workdir = Path(directory)
                (workdir / "rollup.py").write_text(source)
                start = time.perf_counter()
                result = verify(workdir, task["verifier"]["args"])
                record = result.performance
                print(
                    json.dumps(
                        {
                            "candidate": label,
                            "run": run,
                            "elapsed_s": time.perf_counter() - start,
                            "passed": result.passed,
                            "score": result.score,
                            "feedback": result.feedback,
                            "performance": record.model_dump() if record else None,
                        }
                    ),
                    flush=True,
                )
                if record is None or record.correctness is not True:
                    raise SystemExit("calibration correctness or harness failure")
                if result.score != (1 if label == "algorithmic" else 0):
                    raise SystemExit(
                        "unstable calibration bucket: resize before freezing"
                    )


if __name__ == "__main__":
    main()
