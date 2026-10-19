"""Capture the real DR jobs route with invented data, never a production host."""

import argparse
import functools
import http.server
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

# Reuse the existing fixture artifact server and byte-integrity check.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preview"))
from browser_check import PREFIX, Handler, digest_tree

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
INTERVAL = 30 * 60_000


def check(root, output, executable=None):
    before = digest_tree(root)
    output.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}{PREFIX}"
    evidence = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=executable,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
                + (["--no-zygote"] if executable else []),
            )
            for width in [390, 1440]:
                for scheme in ["light", "dark"]:
                    context = browser.new_context(
                        viewport={"width": width, "height": 1000},
                        color_scheme=scheme,
                        reduced_motion="reduce",
                        service_workers="block",
                    )
                    page = context.new_page()
                    errors, failures, forbidden = [], [], []
                    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                    page.on(
                        "console",
                        lambda m, errors=errors: (
                            errors.append(m.text)
                            if m.type in ["error", "warning"]
                            else None
                        ),
                    )
                    page.on(
                        "requestfailed",
                        lambda r, failures=failures: failures.append(r.url),
                    )

                    def route_request(route, forbidden=forbidden):
                        if route.request.url.startswith(base):
                            route.continue_()
                        else:
                            forbidden.append(route.request.url)
                            route.abort()

                    context.route("**/*", route_request)
                    context.tracing.start(
                        screenshots=True, snapshots=True, sources=False
                    )
                    name = f"{width}-{scheme}"
                    record = {
                        "width": width,
                        "scheme": scheme,
                        "fixture": True,
                        "captures": [],
                    }
                    evidence.append(record)
                    page.clock.install(time=NOW)
                    page.clock.pause_at(NOW)

                    def no_overflow(page=page):
                        dimensions = page.evaluate(
                            "({scrollWidth: document.documentElement.scrollWidth, innerWidth})"
                        )
                        assert dimensions["scrollWidth"] <= dimensions["innerWidth"], (
                            dimensions
                        )
                        return dimensions

                    def stable_navigation(page=page):
                        count = page.evaluate("window.drJobsFixture.calls.goto.length")
                        page.wait_for_timeout(100)
                        assert (
                            page.evaluate("window.drJobsFixture.calls.goto.length")
                            == count
                        ), "goto loop"
                        assert page.evaluate(
                            "window.drJobsFixture.calls.goto.every(c => c.options.keepFocus && c.options.noScroll && c.options.replaceState)"
                        )
                        return count

                    def capture(
                        bucket,
                        page=page,
                        name=name,
                        record=record,
                        no_overflow=no_overflow,
                    ):
                        dimensions = no_overflow()
                        image = f"dr-jobs-{name}-{bucket}.png"
                        page.screenshot(path=str(output / image), full_page=True)
                        record["captures"].append(
                            {"bucket": bucket, "file": image, **dimensions}
                        )

                    def focus_visible(locator):
                        locator.focus()
                        assert locator.evaluate("el => el === document.activeElement")
                        assert locator.evaluate(
                            "el => getComputedStyle(el).outlineStyle !== 'none' && parseFloat(getComputedStyle(el).outlineWidth) >= 2"
                        )

                    try:
                        page.goto(base + "?campaign=retained", wait_until="networkidle")
                        page.locator("main.dr-jobs").wait_for()
                        page.evaluate("document.fonts.ready")
                        assert page.evaluate(
                            "document.fonts.check('16px \"Schibsted Grotesk\"')"
                        )
                        assert page.locator("main").count() == 1
                        assert page.locator("button.scheme").count() == 1
                        assert page.locator(".md-nav").count() == 0
                        assert (
                            page.get_by_role(
                                "navigation",
                                name="Site",
                                exact=True,
                                include_hidden=True,
                            ).count()
                            == 1
                        )
                        assert (
                            page.get_by_role("navigation", name="Breadcrumb")
                            .filter(visible=True)
                            .count()
                            == 1
                        )
                        assert (
                            page.title() == "NHS Scotland anaesthetics consultant jobs"
                        )
                        assert page.locator("a.row").count() == 5
                        assert stable_navigation() == 0
                        capture("live")

                        # Measure actual computed text paint, including color-mix
                        # tokens and each element's nearest opaque background.
                        contrast = page.evaluate("""() => {
                          const canvas = document.createElement('canvas');
                          canvas.width = canvas.height = 1;
                          const ctx = canvas.getContext('2d', {willReadFrequently: true});
                          function rgba(color) {
                            ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1);
                            return [...ctx.getImageData(0, 0, 1, 1).data];
                          }
                          function luminance(rgb) {
                            return rgb.slice(0, 3).map(x => x / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4)
                              .reduce((sum, x, i) => sum + x * [.2126, .7152, .0722][i], 0);
                          }
                          return ['.stats', '.source', '.r-title', '.r-meta', '.r-date', '.r-rel.urgent', '.badge.new', '.seg[aria-pressed="true"]', 'label', 'select'].map(selector => {
                            const el = document.querySelector(selector);
                            let ancestor = el, bg;
                            while (ancestor) { bg = rgba(getComputedStyle(ancestor).backgroundColor); if (bg[3] === 255) break; ancestor = ancestor.parentElement; }
                            if (!ancestor) throw new Error('No opaque background: ' + selector);
                            const fg = rgba(getComputedStyle(el).color);
                            const a = luminance(fg), b = luminance(bg);
                            return {selector, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05), fg, bg};
                          });
                        }""")
                        assert all(c["ratio"] >= 4.5 for c in contrast), contrast
                        record["contrast"] = contrast

                        # Native keyboard selection, with no Happy DOM adapter.
                        select = page.get_by_label("Filter by location", exact=True)
                        focus_visible(select)
                        page.keyboard.press("Home")
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                        assert select.input_value() == "Glasgow"
                        assert parse_qs(urlsplit(page.url).query)["town"] == ["Glasgow"]
                        assert page.locator("a.row").count() == 2
                        focus_visible(
                            page.get_by_role("button", name="Past (3)", exact=True)
                        )
                        page.keyboard.press("Enter")
                        assert select.input_value() == ""
                        assert "town" not in parse_qs(urlsplit(page.url).query)
                        assert parse_qs(urlsplit(page.url).query)["view"] == ["history"]
                        assert parse_qs(urlsplit(page.url).query)["campaign"] == [
                            "retained"
                        ]
                        assert page.locator("a.row").count() == 3
                        assert page.locator(".new").count() == 0
                        capture("history")
                        focus_visible(page.locator("a.row").first)
                        page.screenshot(
                            path=str(output / f"dr-jobs-{name}-focus.png"),
                            full_page=True,
                        )
                        stable_navigation()

                        # The real component owns this timer and its teardown.
                        assert (
                            page.evaluate("window.drJobsFixture.calls.invalidations")
                            == 0
                        )
                        page.clock.run_for(INTERVAL - 1)
                        assert (
                            page.evaluate("window.drJobsFixture.calls.invalidations")
                            == 0
                        )
                        page.clock.run_for(1)
                        assert (
                            page.evaluate("window.drJobsFixture.calls.invalidations")
                            == 1
                        )
                        page.evaluate("window.drJobsFixture.dispose()")
                        page.clock.run_for(INTERVAL)
                        assert (
                            page.evaluate("window.drJobsFixture.calls.invalidations")
                            == 1
                        )
                        record["refreshAndCleanup"] = "passed"

                        page.goto(
                            base + "?view=history&town=Edinburgh&campaign=retained",
                            wait_until="networkidle",
                        )
                        assert select.input_value() == "Edinburgh"
                        assert page.locator("a.row").count() == 1
                        assert stable_navigation() == 0
                        toggle = page.locator("button.scheme")
                        focus_visible(toggle)
                        page.keyboard.press("Enter")
                        other = "dark" if scheme == "light" else "light"
                        assert (
                            page.evaluate("localStorage.getItem('td-theme')") == other
                        )
                        page.keyboard.press("Enter")
                        page.reload(wait_until="networkidle")
                        assert (
                            page.locator("html").get_attribute("data-theme") == scheme
                        )
                        page.goto(
                            base + "?view=history&town=Unknown&campaign=retained",
                            wait_until="networkidle",
                        )
                        assert select.input_value() == ""
                        assert page.locator("a.row").count() == 3
                        assert stable_navigation() == 1

                        for scenario, query, message in [
                            (
                                "empty-live",
                                "",
                                "No live anaesthetics consultant posts right now. Try History for recent listings.",
                            ),
                            (
                                "empty-history",
                                "&view=history",
                                "No past listings recorded yet.",
                            ),
                        ]:
                            page.goto(
                                base + "?fixture=" + scenario + query,
                                wait_until="networkidle",
                            )
                            assert page.locator("a.row").count() == 0
                            assert (
                                " ".join(page.locator(".empty").inner_text().split())
                                == message
                            )
                            capture(scenario)
                        record.update(
                            keyboard="passed",
                            urlSynchronization="passed",
                            schemePersistence="passed",
                            errors=errors,
                            failedRequests=failures,
                            forbiddenRequests=forbidden,
                        )
                        assert not errors, errors
                        assert not failures, failures
                        assert not forbidden, forbidden
                        record["status"] = "passed"
                    except Exception:
                        page.screenshot(
                            path=str(output / f"dr-jobs-{name}-failure.png"),
                            full_page=True,
                        )
                        raise
                    finally:
                        context.tracing.stop(
                            path=str(output / f"dr-jobs-{name}-trace.zip")
                        )
                        context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (output / "checks.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert before == digest_tree(root), "browser check changed the preview artifact"
    (output / "tested-files.json").write_text(json.dumps(before, indent=2) + "\n")
    print(
        f"PASS: {len(evidence)} width/scheme variants, 16 captures, native keyboard, URL and refresh checks"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable")
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.executable)
