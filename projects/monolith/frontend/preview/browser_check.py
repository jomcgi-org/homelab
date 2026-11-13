"""Exercise the exact static artifact at a Pages-style nested URL, offline."""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


# Runners are also loaded by file path, without a frontend sys.path entry.
_support_spec = importlib.util.spec_from_file_location(
    "preview_support", Path(__file__).resolve().parent.parent / "preview_support.py"
)
_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(_support)
digest_tree = _support.digest_tree
static_server = _support.static_server


PREFIX = "/homelab/pr/42/" + "a" * 40 + "/"
VIEWPORTS = [(360, 640), (390, 844), (1440, 1000)]


def check_blog(root, output):
    from playwright.sync_api import sync_playwright

    before = digest_tree(root)
    output.mkdir(parents=True, exist_ok=True)
    evidence = []
    try:
        with (
            static_server(root, prefix=PREFIX) as base,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch()
            for width, height in VIEWPORTS:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    reduced_motion="reduce",
                    service_workers="block",
                )
                context.add_init_script("""
                    window.__fixtureCspViolations = [];
                    addEventListener('securitypolicyviolation', event => {
                      window.__fixtureCspViolations.push(event.violatedDirective + ':' + event.blockedURI);
                    });
                """)
                page = context.new_page()
                forbidden = []
                failures = []
                errors = []

                def route_request(route, forbidden=forbidden):
                    url = route.request.url
                    if not url.startswith(base):
                        forbidden.append(url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                page.on(
                    "pageerror",
                    lambda error, errors=errors: errors.append(str(error)),
                )
                page.on(
                    "requestfailed",
                    lambda request, failures=failures: failures.append(request.url),
                )
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                name = f"{width}x{height}"
                try:
                    page.goto(base, wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    assert page.evaluate(
                        """document.fonts.check('16px \"Schibsted Grotesk\"')"""
                    ), "local production font did not load"
                    page.get_by_role(
                        "heading", name="Serving larger-than-memory MoE models"
                    ).first.wait_for()
                    page.get_by_role(
                        "region", name="Inference on the RTX 4090"
                    ).wait_for()
                    page.get_by_text(
                        "Synthetic fixture preview", exact=False
                    ).wait_for()
                    assert not page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"
                    ), "horizontal page overflow"
                    assert (
                        page.locator("#landing-title").bounding_box()["y"] < height / 2
                    )
                    page.screenshot(
                        path=str(output / f"{name}-opening.png"), full_page=False
                    )
                    page.screenshot(
                        path=str(output / f"{name}-full.png"), full_page=True
                    )
                    # Native production controls, no test-only component doubles.
                    play = page.get_by_role("button", name="Play", exact=True)
                    play.click()
                    page.get_by_role("button", name="Pause", exact=True).click()
                    slider = page.get_by_role("slider")
                    slider.focus()
                    slider.press("End")
                    page.get_by_text(
                        "This invented report shows a fictional handoff between a worker and a local queue.",
                        exact=True,
                    ).first.wait_for()
                    assert slider.input_value() == "3000"
                    assert page.locator(".graph-node").count() == 2
                    assert page.get_by_role(
                        "button", name="Replay", exact=True
                    ).is_visible()
                    assert not page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth + 1"
                    ), "completed graph overflows the page"
                    page.screenshot(
                        path=str(output / f"{name}-complete.png"), full_page=True
                    )
                    page.get_by_role("button", name="Replay", exact=True).click()
                    page.get_by_role("button", name="Pause", exact=True).click()
                    page.get_by_role("link", name="Read the post", exact=False).click()
                    page.get_by_role(
                        "heading", name="1. The synthetic setup", exact=True
                    ).wait_for()
                    assert page.evaluate("scrollY > 0")
                    page.get_by_role("button", name="Switch to night scheme").click()
                    assert page.locator("html").get_attribute("data-theme") == "dark"
                    page.reload(wait_until="networkidle")
                    assert page.locator("html").get_attribute("data-theme") == "dark"
                    page.get_by_role("button", name="Switch to day scheme").click()
                    # Production breadcrumbs stay bounded by the fixture harness.
                    page.get_by_role(
                        "link", name="jomcgi.dev", exact=True
                    ).first.click()
                    assert page.url.startswith(base)
                    assert not page.evaluate("window.__fixtureCspViolations"), (
                        "CSP blocked a non-fixture request"
                    )
                    assert not forbidden, f"external/non-preview requests: {forbidden}"
                    assert not failures, f"failed asset requests: {failures}"
                    assert not errors, f"browser errors: {errors}"
                    evidence.append({"viewport": name, "status": "passed"})
                except Exception:
                    page.screenshot(
                        path=str(output / f"{name}-failure.png"), full_page=True
                    )
                    raise
                finally:
                    context.tracing.stop(path=str(output / f"{name}-trace.zip"))
                    context.close()
            browser.close()
    finally:
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert before == digest_tree(root), "browser check changed the publish artifact"
    (output / "tested-files.json").write_text(json.dumps(before, indent=2) + "\n")


def check(root, output):
    check_blog(root, output)
    # compare.py loads this entry point through suite.py. Keep sheet evidence
    # under head/blog/sheets in the exact-head CI upload, while the temporary
    # build stays outside both the checkout and the Pages publication artifact.
    # The subprocess keeps the sheet's opaque iframe contexts independent of
    # the shared suite's service-worker instrumentation.
    frontend = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="sheet-fixture-") as temporary:
        artifact = Path(temporary) / "build"
        subprocess.run(
            [
                "pnpm",
                "exec",
                "vite",
                "build",
                "--config",
                "sheet-preview/vite.config.js",
                "--outDir",
                str(artifact),
            ],
            cwd=frontend,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(frontend / "sheet-preview/browser_check.py"),
                "--artifact",
                str(artifact),
                "--output",
                str(output.resolve() / "sheets"),
            ],
            cwd=frontend,
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve())
