"""Render real sheet routes and layouts with local, deterministic responses."""

import argparse
import functools
import hashlib
import http.server
import json
import re
import subprocess
import threading
import traceback
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
HEIGHT = 1000


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def geometry(page):
    result = page.evaluate("""() => ({
      viewport: {width: innerWidth, height: innerHeight},
      scrollWidth: document.documentElement.scrollWidth,
      sheet: (() => { const s = getComputedStyle(document.querySelector('main.sheet'));
        return {background: s.backgroundColor, ink: s.color, padding: s.padding}; })(),
      iframe: document.querySelector('iframe')?.getBoundingClientRect().toJSON() ?? null
    })""")
    assert result["scrollWidth"] <= result["viewport"]["width"], result
    return result


def focus(page, locator):
    # Enter keyboard modality after the preceding mouse interactions.
    page.keyboard.press("Tab")
    locator.focus()
    result = locator.evaluate("""node => {
      const s = getComputedStyle(node);
      return {visible: node.matches(':focus-visible'), width: s.outlineWidth,
        style: s.outlineStyle, colour: s.outlineColor};
    }""")
    assert result["visible"] and float(result["width"].removesuffix("px")) >= 1, result
    assert result["style"] != "none", result
    return result


def leaderboard_checks(page):
    assert page.locator("main.td.sheet.lb-page").count() == 1
    assert page.locator(".chrome").count() == 1
    assert page.locator(".md-nav, .masthead, .spine").count() == 0
    assert page.get_by_role("heading", name="LLM leaderboard").count() == 1
    assert page.title() == "LLM Leaderboard · jomcgi.dev"
    assert (
        page.locator('link[rel="canonical"]').get_attribute("href")
        == "https://jomcgi.dev/app/llm-leaderboard"
    )
    assert (
        page.get_by_role("navigation", name="You are here").filter(visible=True).count()
        == 1
    )
    history_length = page.evaluate("history.length")
    page.get_by_role("button", name="All", exact=True).click()
    cost = page.locator(".ranked th button").filter(has_text=re.compile(r"^\$ / task"))
    cost.click()
    rows = page.locator(".ranked:has(th[aria-sort]) tbody tr")
    assert rows.first.get_attribute("data-model") == "qwen/fixture-local"
    cost.click()
    assert rows.first.get_attribute("data-model") == "anthropic/fixture-anchor"
    page.get_by_role("button", name="Clear all").click()
    assert page.locator(".empty").inner_text().startswith("No models selected.")
    assert "m=" not in urlsplit(page.url).query
    search = page.get_by_role("searchbox", name="Search models")
    result = {"search_focus": focus(page, search)}
    search.fill("deepseek")
    search.press("Enter")
    assert rows.count() == 1
    assert rows.first.get_attribute("data-model") == "deepseek/fixture-cloud"
    assert "m=deepseek%2Ffixture-cloud" in page.url
    assert page.evaluate("history.length") == history_length
    search.press("Escape")
    assert search.input_value() == ""
    page.get_by_role("button", name="Top picks").click()
    assert "m=" not in urlsplit(page.url).query
    toggle = page.locator("button.scheme")
    result["scheme_focus"] = focus(page, toggle)
    before = geometry(page)["sheet"]["background"]
    toggle.press("Enter")
    page.wait_for_function(
        "before => getComputedStyle(document.querySelector('main.sheet')).backgroundColor !== before",
        arg=before,
    )
    result["scheme_after"] = geometry(page)["sheet"]["background"]
    assert before != result["scheme_after"]
    selected = page.locator("html").get_attribute("data-theme")
    assert page.evaluate("localStorage.getItem('td-theme')") == selected
    page.reload(wait_until="networkidle")
    assert page.locator("html").get_attribute("data-theme") == selected
    assert geometry(page)["sheet"]["background"] == result["scheme_after"]
    return result


def artifact_checks(page, width):
    frame = page.locator("iframe")
    assert frame.get_attribute("sandbox") == "allow-scripts"
    assert frame.get_attribute("src") == "/artifact/fixture/raw"
    assert frame.get_attribute("title") == "artifact"
    assert frame.get_attribute("srcdoc") is None
    assert page.title() == "Artifact"
    assert page.locator(".md-nav, .chrome, .scheme, .frame, .spine").count() == 0
    bounds = geometry(page)["iframe"]
    assert {key: bounds[key] for key in ["x", "y", "width", "height"]} == {
        "x": 0,
        "y": 0,
        "width": width,
        "height": HEIGHT,
    }, bounds
    child = page.frame_locator("iframe")
    assert child.locator("body").get_attribute("data-parent-access") == "blocked"
    assert page.evaluate("window.artifactRan === undefined")
    button = child.get_by_role("button", name="Count locally")
    result = {"button_focus": focus(page, button)}
    button.press("Enter")
    assert child.get_by_role("status").inner_text() == "1 local clicks"
    return result


def check(root, output, executable=None):
    output.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    raw = (HERE / "artifact.html").read_text()
    evidence = {
        "head_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=HERE, text=True
        ).strip(),
        "cases": [],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable)
            evidence["browser"] = browser.version
            for surface in ["leaderboard", "artifact"]:
                for width in [390, 1440]:
                    for scheme in ["light", "dark"]:
                        name = f"{surface}-{width}x{HEIGHT}-{scheme}"
                        record = {"name": name, "issues": []}
                        evidence["cases"].append(record)
                        # Playwright's service-worker blocking init script reads
                        # navigator.serviceWorker inside every frame and throws
                        # in an opaque sandbox origin. Use the default here;
                        # these local fixtures register no service worker.
                        context = browser.new_context(
                            viewport={"width": width, "height": HEIGHT},
                            color_scheme=scheme,
                            reduced_motion="reduce",
                        )
                        errors = []

                        def guard(route, errors=errors):
                            url = route.request.url
                            if url.startswith("https://fonts.googleapis.com/"):
                                # Production layout head is unchanged; vendored fonts supply pixels.
                                route.fulfill(content_type="text/css", body="")
                            elif url.startswith(base + "/artifact/fixture/raw"):
                                route.fulfill(
                                    content_type="text/html",
                                    body=raw,
                                    headers={
                                        "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
                                    },
                                )
                            elif url == base + "/artifact/fixture/version":
                                route.fulfill(json={"version": "fixture-v1"})
                            elif url == base + "/favicon.svg":
                                route.fulfill(status=204)
                            elif url.startswith(base + "/"):
                                route.continue_()
                            else:
                                errors.append(f"non-fixture request: {url}")
                                route.abort()

                        context.route("**/*", guard)
                        context.tracing.start(screenshots=True, snapshots=True)
                        page = context.new_page()
                        page.on(
                            "pageerror",
                            lambda error, errors=errors: errors.append(str(error)),
                        )
                        page.set_default_timeout(10000)
                        try:
                            page.goto(
                                f"{base}/?surface={surface}", wait_until="networkidle"
                            )
                            page.evaluate("document.fonts.ready")
                            record["layout"] = geometry(page)
                            record["accessibility"] = page.locator(
                                "body"
                            ).aria_snapshot()
                            page.screenshot(path=str(output / f"{name}.png"))
                            if surface == "leaderboard":
                                page.screenshot(
                                    path=str(output / f"{name}-full.png"),
                                    full_page=True,
                                )
                                record["interaction"] = leaderboard_checks(page)
                                page.goto(
                                    f"{base}/?surface=leaderboard&empty=1",
                                    wait_until="networkidle",
                                )
                                assert (
                                    page.locator(".empty")
                                    .inner_text()
                                    .startswith("No models selected.")
                                )
                                record["empty_layout"] = geometry(page)
                            else:
                                record["interaction"] = artifact_checks(page, width)
                                record["prefixed_routes"] = []
                                for prefix in ["public", "private"]:
                                    query = urlencode(
                                        {
                                            "surface": "artifact",
                                            "route": f"/{prefix}/artifact/fixture",
                                        }
                                    )
                                    page.goto(
                                        f"{base}/?{query}", wait_until="networkidle"
                                    )
                                    artifact_checks(page, width)
                                    record["prefixed_routes"].append(geometry(page))
                            assert not errors, errors
                            record["status"] = "passed"
                        except (AssertionError, PlaywrightError):
                            record["issues"].append(traceback.format_exc())
                            record["status"] = "failed"
                            page.screenshot(
                                path=str(output / f"{name}-failure.png"), full_page=True
                            )
                        finally:
                            context.tracing.stop(path=str(output / f"{name}-trace.zip"))
                            context.close()
                            print(f"{record['status']}: {name}", flush=True)
                            for issue in record["issues"]:
                                print(issue, flush=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        evidence["screenshots"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.glob("*.png"))
        }
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert len(evidence["cases"]) == 8
    assert all(case["status"] == "passed" for case in evidence["cases"]), (
        "sheet browser checks failed; see result.json"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable")
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.executable)
