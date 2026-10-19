"""Build and capture approved synthetic fixtures on an unprivileged PR runner.

This module is never run by the privileged publisher. Suite commands are fixed
here, and map suites reuse their owning PR's harness without merging that PR.
"""

import argparse
import hashlib
import html
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

import publish

TOOL_ROOT = Path(__file__).resolve().parents[3]
ROOT = TOOL_ROOT
FRONTEND = ROOT / "projects/monolith/frontend"
SUITES = {
    "blog": ("preview", "Blog replay", "interactive"),
    "factory": (
        "factory-preview",
        "Factory overview, activity and context",
        "interactive",
    ),
    "review": ("review-preview", "Private review components", "interactive"),
    "trips": ("trips-preview", "Trips sheets and photo viewer", "gallery"),
    "stars": ("stars-preview", "Stars live and historical map", "gallery"),
}
REQUIRED_SUITES = ("blog", "factory", "review")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def assert_revision(root, sha):
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    require(
        publish.SHA.fullmatch(sha) and actual == sha,
        "checkout does not match expected SHA",
    )
    require(
        not subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            text=True,
        ).strip(),
        "commit source changes before an exact-SHA build",
    )


def digest_tree(root):
    require(root.is_dir(), f"missing artifact directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"symlink in artifact: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    require(result, f"empty artifact: {root}")
    return result


def owner_module(directory):
    path = FRONTEND / directory / "browser_check.py"
    spec = importlib.util.spec_from_file_location(directory.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selected_suites(requested=None):
    names = requested or list(REQUIRED_SUITES) + [
        name for name in ("trips", "stars") if (FRONTEND / SUITES[name][0]).is_dir()
    ]
    require(len(names) == len(set(names)), "duplicate suite")
    for name in names:
        require(name in SUITES, f"unregistered suite: {name}")
        require(
            (FRONTEND / SUITES[name][0] / "vite.config.js").is_file(),
            f"missing {name} owner harness",
        )
    require(names, "zero fixture suites selected")
    return names


def expected_captures(name):
    require(name in SUITES, f"unregistered suite: {name}")
    if name == "blog":
        return {
            f"{size}-{state}.png"
            for size, state in itertools.product(
                ("360x640", "390x844", "1440x1000"), ("opening", "full", "complete")
            )
        }
    if name == "factory":
        # The owner's full interaction/overflow matrix still runs. These are
        # the required visual review captures at the requested widths.
        return {
            f"{view}-live-{size}-{scheme}-100pct-{state}.png"
            for view, size, scheme, state in itertools.product(
                ("overview", "activity", "context", "chapter", "search"),
                ("390x844", "1440x1000"),
                ("light", "dark"),
                ("full", "opening"),
            )
        }
    if name == "review":
        return {
            f"{tab}-{mode}-{width}-{scheme}.png"
            for tab, mode, width, scheme in itertools.product(
                ("gaps", "notes"), ("pending", "audit"), (390, 1440), ("light", "dark")
            )
        }
    if name == "trips":
        base = {
            f"{page}-{width}-{scheme}.png"
            for page, width, scheme in itertools.product(
                ("index", "trip", "day", "timeline"), (390, 1440), ("light", "dark")
            )
        }
        return (
            base
            | {
                f"timeline-{width}-{scheme}-{state}.png"
                for width, scheme, state in itertools.product(
                    (390, 1440), ("light", "dark"), ("viewer", "map")
                )
            }
            | {
                f"day-{width}-{scheme}-{state}.png"
                for width, scheme, state in itertools.product(
                    (390, 1440), ("light", "dark"), ("map-controls", "fullscreen")
                )
            }
        )
    return {
        f"{mode}-{scheme}-{width}.png"
        for mode, scheme, width in itertools.product(
            ("live", "historical"), ("light", "dark"), (390, 1440)
        )
    }


def validate_captures(name, output):
    expected = expected_captures(name)
    for filename in sorted(expected):
        path = output / filename
        require(path.is_file(), f"{name}: missing capture {filename}")
        data = path.read_bytes()
        require(
            len(data) > 32 and data.startswith(b"\x89PNG\r\n\x1a\n"),
            f"{name}: invalid PNG {filename}",
        )
    return {
        filename: hashlib.sha256((output / filename).read_bytes()).hexdigest()
        for filename in sorted(expected)
    }


def page(title, body):
    return (
        publish.trusted_html_head(title)
        + '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow">'
        + "<style>body{font:1rem/1.5 system-ui;margin:2rem auto;padding:0 1rem;max-width:64rem;overflow-wrap:anywhere}img{max-width:100%;height:auto}li{margin-block:1rem}a:focus-visible{outline:3px solid currentColor}code{word-break:break-all}</style>"
        + f"</head><body>{body}</body></html>"
    ).encode()


def compose(directory, built, evidence, sha):
    directory.mkdir(parents=True)
    links = []
    for name, source in built.items():
        _, title, mode = SUITES[name]
        target = directory / "suites" / name
        if mode == "interactive":
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True)
            items = []
            for filename in sorted(expected_captures(name)):
                shutil.copyfile(evidence / name / filename, target / filename)
                label = html.escape(filename.removesuffix(".png"))
                items.append(
                    f'<li><a href="{filename}">{label}</a><br><img loading="lazy" src="{filename}" alt="Synthetic {label}"></li>'
                )
            (target / "index.html").write_bytes(
                page(
                    title,
                    f'<a href="../../">All fixture demos</a><h1>{title}</h1><p>Invented data and offline basemap. Captured controls and viewer states; this gallery has no interactive map.</p><code>{sha}</code><ul>{"".join(items)}</ul>',
                )
            )
        links.append(f'<li><a href="suites/{name}/">{title}</a> ({mode})</li>')
    (directory / "index.html").write_bytes(
        page(
            "Synthetic fixture demos",
            "<h1>Synthetic fixture demos</h1><p>All records are invented. Never enter credentials or personal data.</p>"
            + f"<p>Source commit <code>{sha}</code></p><ul>{''.join(links)}</ul>",
        )
    )
    (directory / "build.json").write_text(
        json.dumps(
            {"commit": sha, "suites": list(built), "scope": "synthetic fixtures only"},
            indent=2,
        )
        + "\n"
    )
    validate_artifact(directory)


def validate_artifact(directory):
    """Use the publisher's unchanged archive/path/CSP/size validation locally."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.writestr(
                    path.relative_to(directory).as_posix(), path.read_bytes()
                )
    publish.extract_assets(stream.getvalue())


def command(arguments, log, env):
    with log.open("w") as handle:
        result = subprocess.run(
            arguments, cwd=FRONTEND, env=env, stdout=handle, stderr=subprocess.STDOUT
        )
    print(log.read_text(), end="", flush=True)
    require(result.returncode == 0, f"command failed ({result.returncode}); see {log}")


def run(output, evidence, sha, requested=None, source=TOOL_ROOT):
    global ROOT, FRONTEND
    ROOT = source
    FRONTEND = ROOT / "projects/monolith/frontend"
    import capture

    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    require(
        publish.SHA.fullmatch(sha) and actual == sha,
        "checkout does not match expected SHA",
    )
    # A SHA label must identify the source that was actually built.
    require(
        not subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT,
            text=True,
        ).strip(),
        "commit source changes before an exact-SHA build",
    )
    require(
        not output.exists() and not evidence.exists(),
        "use fresh output and evidence directories",
    )
    names = selected_suites(requested)
    evidence.mkdir(parents=True)
    runner_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=TOOL_ROOT, text=True
    ).strip()
    assert_revision(TOOL_ROOT, runner_sha)
    receipt = {
        "commit": sha,
        "runner_commit": runner_sha,
        "suites": [],
        "status": "failed",
    }
    env = {**os.environ, "FACTORY_PREVIEW_SHA": sha, "STARS_PREVIEW_SHA": sha}
    original_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="fixture-suite-") as temporary:
            built = {}
            for name in names:
                directory, _, mode = SUITES[name]
                record = {
                    "suite": name,
                    "commit": sha,
                    "mode": mode,
                    "status": "failed",
                }
                receipt["suites"].append(record)
                destination = Path(temporary) / name
                suite_evidence = evidence / name
                suite_evidence.mkdir()
                config = FRONTEND / directory / "vitest.config.js"
                if config.is_file():
                    command(
                        [
                            "pnpm",
                            "exec",
                            "vitest",
                            "run",
                            "--config",
                            f"{directory}/vitest.config.js",
                        ],
                        suite_evidence / "contract.log",
                        env,
                    )
                elif name in {"trips", "stars"}:
                    paths = (
                        ["src/routes/public/app/trips", "src/lib/trips"]
                        if name == "trips"
                        else ["src/routes/public/app/stars", "src/lib/public/stars"]
                    )
                    command(
                        ["pnpm", "exec", "vitest", "run", *paths, "--maxWorkers=2"],
                        suite_evidence / "contract.log",
                        env,
                    )
                command(
                    [
                        "pnpm",
                        "exec",
                        "vite",
                        "build",
                        "--config",
                        f"{directory}/vite.config.js",
                        "--outDir",
                        str(destination),
                    ],
                    suite_evidence / "build.log",
                    env,
                )
                before = digest_tree(destination)
                # Owner checkers with relative git/module paths run at frontend.
                os.chdir(FRONTEND)
                with capture.instrument(suite_evidence, record, maps=mode == "gallery"):
                    if name == "review":
                        capture.review(destination, suite_evidence)
                    else:
                        owner = owner_module(directory)
                        if name == "blog":
                            owner.check(destination, suite_evidence)
                        elif name == "trips":
                            owner.check(destination, suite_evidence, sha)
                        else:
                            owner.check(destination, suite_evidence, expected_sha=sha)
                require(
                    before == digest_tree(destination),
                    f"{name}: capture modified built files",
                )
                record["screenshots"] = validate_captures(name, suite_evidence)
                record["tested_files"] = before
                record["status"] = "passed"
                built[name] = destination
            compose(output, built, evidence, sha)
            before = digest_tree(output)
            with capture.instrument(evidence / "aggregate", receipt):
                capture.aggregate(output, evidence / "aggregate", names)
            require(
                before == digest_tree(output),
                "aggregate browser check changed publish bytes",
            )
            assert_revision(ROOT, sha)
            assert_revision(TOOL_ROOT, runner_sha)
            receipt["tested_files"] = before
            receipt["status"] = "passed"
    except Exception as error:
        receipt["error"] = str(error)
        # A failed suite must never leave a publishable partial aggregate.
        if output.exists():
            shutil.rmtree(output)
        raise
    finally:
        os.chdir(original_cwd)
        (evidence / "suite.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--suite", action="append", choices=SUITES)
    parser.add_argument(
        "--source",
        type=Path,
        default=TOOL_ROOT,
        help="Read-only owner checkout for local exact-head evidence; CI uses its current checkout",
    )
    args = parser.parse_args()
    run(
        args.output.resolve(),
        args.evidence.resolve(),
        args.expected_sha,
        args.suite,
        args.source.resolve(),
    )
