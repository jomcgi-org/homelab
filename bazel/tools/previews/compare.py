"""Capture a PR and its real merge base, then assemble matched PNG comparisons.

Runs only in the read-only build lane. The publisher consumes its output as
validated static data and never executes this or either source revision.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import tempfile

import publish
import suite

ROOT = Path(__file__).resolve().parents[3]
EXTRA_FIXTURES = {
    "trips": [
        "test/trips-fixtures.js",
        "test/TripsRoute.svelte",
        "test/profile-page.svelte.js",
        "test/profile-stores.js",
        "test/app-environment-stub.js",
        "preview/fonts.css",
        "preview/fonts/SchibstedGrotesk.ttf",
    ],
    "stars": [
        "test/stars/fixtures.js",
        "test/stars/Route.svelte",
        "test/profile-page.svelte.js",
        "test/profile-stores.js",
        "preview/fonts.css",
        "preview/fonts/SchibstedGrotesk.ttf",
    ],
    "review": ["preview/fonts.css", "preview/fonts/SchibstedGrotesk.ttf"],
    "factory": ["factory-preview/.generated/pages.json"],
}


def png_dimensions(path, expected_width):
    data = path.read_bytes()
    width, height = struct.unpack("!II", data[16:24])
    suite.require(
        width == expected_width and height > 0,
        "capture dimensions do not match declared viewport",
    )
    return {"width": width, "height": height}


def fingerprint(source, name, *, include_generated=True):
    frontend = source / "projects/monolith/frontend"
    directory = frontend / suite.SUITES[name][0]
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not {"__pycache__", ".generated"}.intersection(path.parts)
        and path.suffix
        in {".js", ".mjs", ".svelte", ".json", ".css", ".html", ".py", ".ttf"}
    ]
    files += [
        frontend / relative
        for relative in EXTRA_FIXTURES.get(name, [])
        if include_generated or ".generated" not in Path(relative).parts
    ]
    suite.require(
        files and all(path.is_file() for path in files),
        f"{name}: fixture inputs missing",
    )
    manifest = {
        path.relative_to(frontend).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(files)
    }
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def baseline_plan(head_source, base_source, names):
    """Do not apply today's capture matrix to an incompatible old harness."""
    inputs = {}
    eligible = []
    for name in names:
        record = inputs[name] = {
            "head": fingerprint(head_source, name, include_generated=False),
            "before": None,
        }
        harness = base_source / "projects/monolith/frontend" / suite.SUITES[name][0]
        if not (harness / "vite.config.js").is_file():
            continue
        try:
            record["before"] = fingerprint(base_source, name, include_generated=False)
        except ValueError:
            record["unavailable"] = (
                "Baseline harness exists but required synthetic fixture inputs are missing. "
                "No comparable before capture is available."
            )
        if record["before"] == record["head"]:
            eligible.append(name)
    return inputs, eligible


def case_details(name, filename):
    if name == "blog":
        match = re.fullmatch(r"(\d+)x\d+-(opening|full|complete)\.png", filename)
        return match[2], int(match[1]), "light"
    if name == "factory":
        match = re.fullmatch(
            r"(.+)-live-(\d+)x\d+-(light|dark)-100pct-(full|opening)\.png", filename
        )
        return f"{match[1]}-{match[4]}", int(match[2]), match[3]
    if name == "review":
        match = re.fullmatch(
            r"(gaps|notes)-(pending|audit)-(\d+)-(light|dark)\.png", filename
        )
        return f"{match[1]}-{match[2]}", int(match[3]), match[4]
    if name == "trips":
        match = re.fullmatch(
            r"(index|trip|day|timeline)-(\d+)-(light|dark)(?:-(viewer|map|map-controls|fullscreen))?\.png",
            filename,
        )
        return match[1] + (f"-{match[4]}" if match[4] else ""), int(match[2]), match[3]
    match = re.fullmatch(r"(live|historical)-(light|dark)-(\d+)\.png", filename)
    return match[1], int(match[3]), match[2]


def assemble(
    output,
    head_artifact,
    base_artifact,
    evidence,
    head,
    baseline,
    names,
    base_names,
    fingerprints,
):
    suite.require(
        publish.SHA.fullmatch(head) and publish.SHA.fullmatch(baseline),
        "invalid comparison revisions",
    )
    output.mkdir()
    shutil.copytree(head_artifact, output / "after")
    if base_names:
        shutil.copytree(base_artifact, output / "before")
    head_receipt = json.loads((evidence / "head/suite.json").read_text())
    base_receipt = (
        json.loads((evidence / "before/suite.json").read_text())
        if base_names
        else {"suites": []}
    )
    suite.require(
        head_receipt["commit"] == head and head_receipt["status"] == "passed",
        "head evidence mismatch",
    )
    if base_names:
        suite.require(
            base_receipt["commit"] == baseline and base_receipt["status"] == "passed",
            "baseline evidence mismatch",
        )
    head_records = {record["suite"]: record for record in head_receipt["suites"]}
    base_records = {record["suite"]: record for record in base_receipt["suites"]}
    model = {
        "head": head,
        "baseline": baseline,
        "baseline_kind": "git merge-base",
        "rows": [],
        "suites": {name: suite.SUITES[name][1] for name in names},
    }
    for name in names:
        after_record = head_records[name]
        before_record = base_records.get(name)
        reason = None
        if fingerprints[name].get("unavailable"):
            reason = fingerprints[name]["unavailable"]
        elif (
            fingerprints[name]["before"] is not None
            and fingerprints[name]["head"] != fingerprints[name]["before"]
        ):
            reason = "Synthetic inputs or capture harness differ at the baseline. These captures are not labeled as a matched pair."
        elif before_record is None:
            reason = f"Baseline has no {suite.SUITES[name][0]} harness. No matching before capture exists."
        elif any(
            after_record[key] != before_record[key]
            for key in ("playwright", "chromium")
        ):
            reason = "Browser versions differ. These captures are not labeled as a matched pair."
        suite.validate_captures(name, evidence / "head" / name)
        if before_record:
            suite.validate_captures(name, evidence / "before" / name)
        for filename in sorted(suite.expected_captures(name)):
            case, width, scheme = case_details(name, filename)
            row = {
                "suite": name,
                "case": case,
                "width": width,
                "scheme": scheme,
                "after": f"captures/{name}/after-{filename}",
                "before": None,
                "unavailable": reason,
                "identical": False,
                "after_dimensions": png_dimensions(
                    evidence / "head" / name / filename, width
                ),
                "before_dimensions": None,
                "interactive_after": f"after/suites/{name}/",
                "interactive_before": f"before/suites/{name}/"
                if before_record
                else None,
            }
            target = output / "captures" / name
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(evidence / "head" / name / filename, output / row["after"])
            if reason is None:
                row["before"] = f"captures/{name}/before-{filename}"
                row["before_dimensions"] = png_dimensions(
                    evidence / "before" / name / filename, width
                )
                shutil.copyfile(
                    evidence / "before" / name / filename, output / row["before"]
                )
                row["identical"] = (
                    after_record["screenshots"][filename]
                    == before_record["screenshots"][filename]
                )
            model["rows"].append(row)
    data = json.dumps(model, sort_keys=True).replace("<", "\\u003c")
    (output / "index.html").write_text(
        Path(__file__)
        .with_name("comparison.html")
        .read_text()
        .replace("FIXTURE_COMPARISON_DATA", data)
    )
    (output / "comparison.json").write_text(
        json.dumps({**model, "fixture_fingerprints": fingerprints}, indent=2) + "\n"
    )
    # Identical bytes from each independently checked artifact remain nested.
    suite.validate_artifact(output)
    return model


def run(output, evidence, head, base_sha):
    import capture

    suite.require(
        publish.SHA.fullmatch(head) and publish.SHA.fullmatch(base_sha),
        "full source/base SHAs required",
    )
    suite.require(
        not output.exists() and not evidence.exists(),
        "use fresh output and evidence directories",
    )
    baseline = subprocess.check_output(
        ["git", "merge-base", head, base_sha], cwd=ROOT, text=True
    ).strip()
    suite.require(publish.SHA.fullmatch(baseline), "merge base unavailable")
    evidence.mkdir(parents=True)
    receipt = {
        "head": head,
        "baseline": baseline,
        "base_ref_sha": base_sha,
        "status": "failed",
    }
    registered = False
    try:
        with tempfile.TemporaryDirectory(prefix="fixture-comparison-") as temporary:
            root = Path(temporary)
            source = root / "baseline-source"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(source), baseline],
                cwd=ROOT,
                check=True,
            )
            registered = True
            try:
                names = suite.selected_suites()
                inputs, base_names = baseline_plan(ROOT, source, names)
                suite.run(root / "head-artifact", evidence / "head", head, names, ROOT)
                if base_names:
                    # Install the baseline's own frozen lock. No production or
                    # publication credential is supplied to either revision.
                    with (evidence / "baseline-install.log").open("w") as log:
                        result = subprocess.run(
                            [
                                "pnpm",
                                "install",
                                "--frozen-lockfile",
                                "--ignore-scripts",
                            ],
                            cwd=source,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                        )
                    suite.require(
                        result.returncode == 0,
                        "baseline frozen dependency install failed; see baseline-install.log",
                    )
                    suite.run(
                        root / "base-artifact",
                        evidence / "before",
                        baseline,
                        base_names,
                        source,
                    )
                # Generated fixture data is available only after each contract
                # checker runs. Compare it too before pairing eligible captures.
                for name in base_names:
                    inputs[name] = {
                        "head": fingerprint(ROOT, name),
                        "before": fingerprint(source, name),
                    }
                suite.assert_revision(ROOT, head)
                model = assemble(
                    output,
                    root / "head-artifact",
                    root / "base-artifact",
                    evidence,
                    head,
                    baseline,
                    names,
                    base_names,
                    inputs,
                )
                before = suite.digest_tree(output)
                with capture.instrument(evidence / "comparison", receipt):
                    capture.comparison(output, evidence / "comparison", model)
                suite.require(
                    before == suite.digest_tree(output),
                    "comparison checks changed public bytes",
                )
                suite.assert_revision(ROOT, head)
                receipt.update(
                    status="passed",
                    tested_files=before,
                    matched_pairs=sum(
                        row["before"] is not None for row in model["rows"]
                    ),
                    unavailable_pairs=sum(
                        row["before"] is None for row in model["rows"]
                    ),
                )
            finally:
                if registered:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(source)],
                        cwd=ROOT,
                        check=True,
                    )
    except Exception as error:
        receipt["error"] = str(error)
        if output.exists():
            shutil.rmtree(output)
        raise
    finally:
        os.chdir(ROOT)
        (evidence / "comparison.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    args = parser.parse_args()
    run(
        args.output.resolve(), args.evidence.resolve(), args.expected_sha, args.base_sha
    )
