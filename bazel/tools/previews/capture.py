"""Shared offline browser instrumentation and fixture aggregate acceptance."""

from contextlib import contextmanager
import functools
import http.server
import importlib.metadata
import itertools
import json
import threading
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import Browser, BrowserType, sync_playwright

from suite import expected_captures

STYLE = {
    "version": 8,
    "sources": {},
    "layers": [
        {
            "id": "background",
            "type": "background",
            "paint": {"background-color": "white"},
        }
    ],
}
MAP_STYLES = {
    "https://tiles.openfreemap.org/styles/liberty",
    "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
}


@contextmanager
def instrument(output, record, maps=False):
    """Keep the owner's checks and add versions, failure traces and offline maps.

    Only the unprivileged capture runner uses this. The public gallery contains
    PNGs, never these network shims or a map worker. No CSP changes are needed.
    """
    output.mkdir(parents=True, exist_ok=True)
    record["playwright"] = importlib.metadata.version("playwright")
    record["offline_map"] = maps
    launch = BrowserType.launch
    new_context = Browser.new_context
    contexts = []
    unexpected = []

    def tracked_launch(browser_type, *args, **kwargs):
        browser = launch(browser_type, *args, **kwargs)
        record["chromium"] = browser.version
        return browser

    def tracked_context(browser, *args, **kwargs):
        kwargs["service_workers"] = "block"
        context = new_context(browser, *args, **kwargs)
        ordinal = len(contexts)
        contexts.append(context)
        if maps:
            # The map owners do not start traces. Keep every context's trace,
            # including incomplete cases, independently of their screenshots.
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            close = context.close

            def traced_close(*args, **kwargs):
                context.tracing.stop(path=str(output / f"context-{ordinal}-trace.zip"))
                return close(*args, **kwargs)

            context.close = traced_close

        def offline(route):
            url = route.request.url
            parsed = urlsplit(url)
            if parsed.hostname == "127.0.0.1" and parsed.scheme == "http":
                route.continue_()
            elif maps and url in MAP_STYLES:
                route.fulfill(content_type="application/json", body=json.dumps(STYLE))
            elif maps and parsed.hostname == "fonts.googleapis.com":
                route.fulfill(content_type="text/css", body="")
            else:
                unexpected.append(url)
                route.abort()

        context.route("**/*", offline)
        return context

    try:
        with (
            patch.object(BrowserType, "launch", tracked_launch),
            patch.object(Browser, "new_context", tracked_context),
        ):
            yield
        assert record.get("chromium"), "no Chromium execution recorded"
        assert contexts, "zero browser contexts ran"
        assert not unexpected, f"non-fixture requests: {unexpected}"
    finally:
        record["contexts"] = len(contexts)
        record["unexpected_requests"] = unexpected


@contextmanager
def serve(root):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root.parent))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{root.name}/"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def checked_context(browser, output, name, width, scheme):
    context = browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=scheme,
        reduced_motion="reduce",
    )
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda request: errors.append(request.url))
    context.add_init_script(
        "window.fixtureViolations=[];addEventListener('securitypolicyviolation',e=>window.fixtureViolations.push(e.violatedDirective))"
    )
    try:
        yield page
        assert not errors, errors
        assert not page.evaluate("window.fixtureViolations"), "CSP violations"
    except Exception:
        page.screenshot(path=str(output / f"{name}-failure.png"), full_page=True)
        context.tracing.stop(path=str(output / f"{name}-trace.zip"))
        raise
    else:
        context.tracing.stop()
    finally:
        context.close()


def measure(page):
    measurement = page.evaluate(
        "({viewport:innerWidth, document:document.documentElement.scrollWidth, body:document.body.scrollWidth})"
    )
    assert (
        max(measurement["document"], measurement["body"]) <= measurement["viewport"] + 1
    ), measurement
    return measurement


def review(root, output):
    results = []
    try:
        with serve(root) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for tab, mode, width, scheme in itertools.product(
                ("gaps", "notes"), ("pending", "audit"), (390, 1440), ("light", "dark")
            ):
                name = f"{tab}-{mode}-{width}-{scheme}"
                with checked_context(browser, output, name, width, scheme) as page:
                    page.goto(base, wait_until="networkidle")
                    page.evaluate("document.fonts.ready")
                    page.get_by_role("navigation", name="Record type").get_by_role(
                        "button", name=tab.capitalize()
                    ).click()
                    if mode == "audit":
                        page.get_by_role("tab", name="Audit auto-decisions").click()
                    before = measure(page)
                    frame = page.get_by_role("region", name="Desktop review controls")
                    frame_widths = frame.evaluate(
                        "e=>({visible:e.clientWidth, content:e.scrollWidth})"
                    )
                    page.screenshot(path=str(output / (name + ".png")), full_page=True)
                    button = page.get_by_role(
                        "button",
                        name="Agree (y)"
                        if mode == "audit"
                        else ("Keep (y)" if tab == "gaps" else "Public (y)"),
                    )
                    button.focus()
                    page.keyboard.press("Enter")
                    assert (
                        page.locator("[data-decision]").inner_text()
                        == "Synthetic decision: yes"
                    )
                    if mode == "audit":
                        page.get_by_role("button", name="Delete (d)").click()
                        page.get_by_role("button", name="Undo (u)").click()
                        assert page.locator("article.card").is_visible()
                    results.append(
                        {
                            "name": name,
                            "before": before,
                            "desktop_frame": frame_widths,
                            "after": measure(page),
                            "status": "passed",
                            "keyboard_decision": True,
                            "delete_undo": mode == "audit",
                        }
                    )
            browser.close()
    finally:
        (output / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
    assert len(results) == len(expected_captures("review")), (
        "private review matrix incomplete"
    )


def aggregate(root, output, names):
    results = []
    try:
        with serve(root) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width in (390, 1440):
                with checked_context(
                    browser, output, str(width), width, "light"
                ) as page:
                    page.goto(base, wait_until="networkidle")
                    assert page.locator("li a").count() == len(names), (
                        "missing suite navigation"
                    )
                    measure(page)
                    page.screenshot(path=str(output / f"{width}.png"), full_page=True)
                    for name in names:
                        page.goto(base + f"suites/{name}/", wait_until="networkidle")
                        if name in {"trips", "stars"}:
                            # Lazy images are scrolled into view before validation.
                            for img in page.locator("img").all():
                                img.scroll_into_view_if_needed()
                                img.evaluate("e=>e.decode()")
                                assert img.evaluate("e=>e.naturalWidth>0"), (
                                    "missing gallery PNG"
                                )
                        measure(page)
                        results.append(
                            {"suite": name, "width": width, "status": "passed"}
                        )
            browser.close()
    finally:
        (output / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
    assert len(results) == 2 * len(names), "aggregate matrix incomplete"
