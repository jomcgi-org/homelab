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
                    assert page.evaluate(
                        "document.body.classList.contains('dark')"
                    ) == (scheme == "dark"), (
                        "private scheme did not follow browser preference"
                    )
                    paint = page.locator("article.card").evaluate(
                        "e=>({background:getComputedStyle(e).backgroundColor,ink:getComputedStyle(e).color})"
                    )
                    scaffold = page.locator("main").evaluate(
                        "e=>getComputedStyle(e).color"
                    )
                    assert scaffold == paint["ink"], "private scaffold scheme mismatch"
                    assert paint["background"] == (
                        "rgb(0, 0, 0)" if scheme == "dark" else "rgb(255, 255, 255)"
                    ), paint
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
                            "paint": paint,
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


def comparison(root, output, model):
    """Check real comparison controls, source labels and preserved scroll."""
    results = []
    with serve(root) as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, scheme in itertools.product((390, 1440), ("light", "dark")):
            with checked_context(
                browser, output, f"comparison-{width}-{scheme}", width, scheme
            ) as page:
                page.goto(base, wait_until="networkidle")
                page.wait_for_function("document.body.dataset.ready")
                assert page.locator("#head").inner_text() == model["head"]
                assert page.locator("#baseline").inner_text() == model["baseline"]
                assert page.locator("#suite option").count() == len(model["suites"])
                # Decode every referenced PNG, including cases outside this
                # control sample. A missing side is a build failure.
                sources = sorted(
                    {
                        row[side]
                        for row in model["rows"]
                        for side in ("before", "after")
                        if row[side]
                    }
                )
                decoded = page.evaluate(
                    "async sources=>{for(const src of sources){const image=new Image();image.src=src;await image.decode();if(!image.naturalWidth)throw Error(src)}return sources.length}",
                    sources,
                )
                assert decoded == len(sources)

                def select(identifier, value):
                    previous = page.locator("body").get_attribute("data-ready")
                    page.locator("#" + identifier).select_option(value)
                    page.wait_for_function(
                        "old=>document.body.dataset.ready!==old", arg=previous
                    )

                matched = next(
                    (
                        row
                        for row in model["rows"]
                        if row["before"]
                        and row["width"] == width
                        and row["scheme"] == scheme
                        and row["case"] in {"full", "overview-full"}
                    ),
                    None,
                )
                position = restored = None
                if matched:
                    select("suite", matched["suite"])
                    select("case", matched["case"])
                    select("width", str(matched["width"]))
                    select("scheme", matched["scheme"])
                    after = page.get_by_role("radio", name="After", exact=True)
                    after.focus()
                    position = page.locator("#viewport").evaluate(
                        "e=>{e.scrollTop=Math.min(120,e.scrollHeight-e.clientHeight);window.scrollTo(0,40);return {top:e.scrollTop,left:e.scrollLeft,y:scrollY}}"
                    )
                    assert position["top"] > 0, (
                        "position test needs a scrollable matched capture"
                    )
                    previous = page.locator("body").get_attribute("data-ready")
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_function(
                        "old=>document.body.dataset.ready!==old", arg=previous
                    )
                    assert page.get_by_role(
                        "radio", name="Before", exact=True
                    ).is_checked()
                    assert (
                        page.locator("#capture").get_attribute("src")
                        == matched["before"]
                    )
                    restored = page.locator("#viewport").evaluate(
                        "e=>({top:e.scrollTop,left:e.scrollLeft,y:scrollY})"
                    )
                    assert restored == position, (position, restored)
                    previous = page.locator("body").get_attribute("data-ready")
                    page.keyboard.press("ArrowRight")
                    page.wait_for_function(
                        "old=>document.body.dataset.ready!==old", arg=previous
                    )
                    assert (
                        page.locator("#capture").get_attribute("src")
                        == matched["after"]
                    )
                    assert (
                        page.locator("#viewport").evaluate(
                            "e=>({top:e.scrollTop,left:e.scrollLeft,y:scrollY})"
                        )
                        == position
                    )
                measure(page)
                page.screenshot(
                    path=str(output / f"comparison-{width}-{scheme}.png"),
                    full_page=True,
                )
                unavailable = next(
                    (row for row in model["rows"] if not row["before"]), None
                )
                if unavailable:
                    select("suite", unavailable["suite"])
                    select("case", unavailable["case"])
                    select("width", str(unavailable["width"]))
                    select("scheme", unavailable["scheme"])
                    previous = page.locator("body").get_attribute("data-ready")
                    page.get_by_role("radio", name="Before", exact=True).check()
                    page.wait_for_function(
                        "old=>document.body.dataset.ready!==old", arg=previous
                    )
                    assert page.locator("#capture").is_hidden()
                    assert not page.locator("#capture").get_attribute("src")
                    assert (
                        "Before unavailable"
                        in page.locator("#unavailable").inner_text()
                    )
                results.append(
                    {
                        "width": width,
                        "browser_scheme": scheme,
                        "decoded_images": decoded,
                        "position": position,
                        "restored_position": restored,
                        "keyboard_toggle": matched is not None,
                        "unavailable_checked": unavailable is not None,
                        "status": "passed",
                    }
                )
        browser.close()
    (output / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
    assert len(results) == 4, "comparison browser matrix incomplete"
