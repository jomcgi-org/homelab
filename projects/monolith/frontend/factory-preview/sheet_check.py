"""Matched synthetic sheet captures with production layouts and stylesheet cascade.

The same head-owned harness can build against SHEET_SOURCE_ROOT at the base
revision. It never imports a loader or makes an external request. Operational
acceptance on the real hosts remains separate.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

VIEWS = [
    "blog",
    "blog-empty",
    "post",
    "single",
    "landing",
    "overview",
    "context",
    "chapter",
    "search",
    "activity",
    "task",
    "attempt",
    "work-item",
    "updates",
    "updates-filter",
    "updates-empty",
    "updates-filter-empty",
]
INIT_SCRIPT = """(() => {
  const RealDate = Date, fixed = Date.parse('2026-10-03T12:00:00Z');
  window.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [fixed])); } static now() { return fixed; } };
  const Original = IntersectionObserver;
  window.__observers = {created:0,disconnected:0};
  window.IntersectionObserver = class extends Original {
    constructor(...args) { super(...args); window.__observers.created++; }
    disconnect() { window.__observers.disconnected++; super.disconnect(); }
  };
})()"""
GEOMETRY = """() => {
  const properties = ['overflowX','overflowY','position','top','fontSize','fontFamily','lineHeight','color','backgroundColor','display','gridTemplateColumns','gap','padding','margin','borderTopWidth','borderBottomWidth','borderLeftWidth','borderRightWidth'];
  const selectors = ['html','body','main','.maintenance-banner','.frame','.journal','.spine','.toc','.date-rail','.edition','.ed-head','.ed-lead','.post-body','.spec','.filters','.masthead'];
  return {
    document: {scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, scrollHeight: document.documentElement.scrollHeight, clientHeight: document.documentElement.clientHeight},
    elements: selectors.flatMap(selector => [...document.querySelectorAll(selector)].map((node, index) => {
      const box = node.getBoundingClientRect(), style = getComputedStyle(node);
      return {selector, index, x:box.x, y:box.y, width:box.width, height:box.height,
        scrollWidth:node.scrollWidth, clientWidth:node.clientWidth, scrollHeight:node.scrollHeight, clientHeight:node.clientHeight,
        style:Object.fromEntries(properties.map(key => [key, style[key]]))};
    }))
  };
}"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def capture(artifact, output, label, *, acceptance=False):
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    build = json.loads((artifact / "sheet-build.json").read_text())
    assert build["fixture_only"] and build["production_sha"] == label, (label, build)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(artifact.resolve()))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    records = []
    try:
        with sync_playwright() as pw:
            # Pin the raster path identically for both source revisions.
            # Repeatability probes below retain compositor variance without
            # masking pixels or modifying the production stylesheet.
            browser = pw.chromium.launch(args=["--disable-gpu"])
            cases = [
                (view, banner)
                for view in VIEWS
                for banner in (
                    [False, True] if view in ["blog", "post", "single"] else [False]
                )
            ]
            for view, banner in cases:
                for width in [390, 1440]:
                    for scheme in ["light", "dark"]:
                        key = f"{view}{'-banner' if banner else ''}-{width}-{scheme}"
                        print(key, flush=True)
                        context = browser.new_context(
                            viewport={"width": width, "height": 900},
                            color_scheme=scheme,
                            reduced_motion="reduce",
                            device_scale_factor=1,
                        )
                        context.add_init_script(INIT_SCRIPT)
                        page = context.new_page()
                        errors, external = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        # Deny even layout font hints. The local font is the only
                        # font used and both revisions have identical bytes.
                        def route(request_route):
                            if request_route.request.url.startswith(
                                f"http://127.0.0.1:{server.server_port}/"
                            ):
                                request_route.continue_()
                            else:
                                external.append(request_route.request.url)
                                request_route.abort()

                        page.route("**/*", route)
                        url = f"http://127.0.0.1:{server.server_port}/sheets.html?view={view}{'&banner=1' if banner else ''}"
                        page.goto(url)
                        page.wait_for_selector("main")
                        page.evaluate("document.fonts.ready")
                        if view == "landing":
                            page.wait_for_selector(".replay")
                        page.wait_for_timeout(100)
                        geometry = page.evaluate(GEOMETRY)
                        page.screenshot(
                            path=str(output / f"{key}.png"),
                            full_page=True,
                            animations="disabled",
                        )
                        opening = {
                            "top": page.evaluate("scrollY"),
                            "geometry": geometry,
                        }
                        page.evaluate(
                            "window.scrollTo(0, document.documentElement.scrollHeight)"
                        )
                        page.wait_for_timeout(50)
                        scrolled = page.evaluate(GEOMETRY)
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(300)
                        checks = {}
                        if view.startswith("updates"):
                            checks["links"] = page.locator("main a").evaluate_all(
                                "nodes => nodes.map(node => node.getAttribute('href'))"
                            )
                            if acceptance:
                                if view != "updates-empty":
                                    assert page.locator(
                                        'a[href*="month=2026-09"]'
                                    ).count(), key
                                assert (
                                    page.locator('a[href*="project=monolith"]').count()
                                    or "filter" in view
                                ), key
                            if view == "updates-filter":
                                page.locator(".spine-group summary").filter(
                                    has_text="Technologies"
                                ).click()
                                page.locator(
                                    '.spine-list a[href*="technology=frontend"]'
                                ).click()
                                page.get_by_role(
                                    "link", name="Open September 2026"
                                ).click()
                                checks["clickedLinks"] = page.evaluate(
                                    "window.__fixtureLinks"
                                )
                                assert any(
                                    "technology=frontend" in link
                                    and "project=monolith" in link
                                    for link in checks["clickedLinks"]
                                ), key
                                assert any(
                                    "month=2026-09" in link
                                    and "project=monolith" in link
                                    for link in checks["clickedLinks"]
                                ), key
                        if view == "post":
                            # Read the click's immediate history update. Once
                            # scrolling settles, the production reading-band
                            # observer may correctly choose the next heading.
                            checks["tocHash"] = page.locator(
                                '.toc a[href="#2-what-to-inspect"]'
                            ).evaluate(
                                "node => { node.click(); return location.hash; }"
                            )
                            assert checks["tocHash"] == "#2-what-to-inspect", key
                        if view == "landing":
                            page.get_by_role(
                                "button", name="Agentic coding", exact=True
                            ).click()
                            assert (
                                page.get_by_role(
                                    "button", name="Agentic coding", exact=True
                                ).get_attribute("aria-pressed")
                                == "true"
                            ), key
                        if view == "attempt":
                            page.locator("details.ask summary").click()
                            assert page.locator("details.ask[open]").count() == 1, key
                            page.get_by_role("button", name="chars +").click()
                            assert (
                                page.locator('.more-tog[aria-expanded="true"]').count()
                                == 1
                            ), key
                        if view == "task":
                            page.get_by_role(
                                "button", name="implement, done, open step 1"
                            ).click()
                            assert page.evaluate("location.hash") == "#step-1", key
                        if acceptance:
                            assert geometry["document"]["scrollWidth"] <= width, (
                                key,
                                geometry["document"],
                            )
                            assert (
                                page.evaluate("document.documentElement.scrollWidth")
                                <= width
                            ), (key, "interaction overflow")
                        if view in ["task", "attempt", "landing", "updates-filter"]:
                            page.screenshot(
                                path=str(output / f"{key}-interaction.png"),
                                full_page=True,
                                animations="disabled",
                            )
                        assert not errors, (key, errors)
                        assert not page.evaluate("window.__fixtureUnexpectedFetches"), (
                            key
                        )
                        page.evaluate("window.__fixtureUnmount()")
                        page.wait_for_timeout(20)
                        observer = page.evaluate("window.__observers")
                        assert observer["created"] == observer["disconnected"], (
                            key,
                            observer,
                        )
                        probes = [f"{key}.png"]
                        records.append(
                            {
                                "key": key,
                                "opening": opening,
                                "scrolled": scrolled,
                                "checks": checks,
                                "observers": observer,
                                "blocked_external_hints": external,
                                "repeatability_images": probes,
                            }
                        )
                        context.close()
                        # Fractional sticky/backdrop layers can snap a glyph
                        # differently on two renders of the unchanged base.
                        # Keep fresh-context probes for these mobile journals.
                        # Acceptance still requires an exact whole-image match
                        # between a real base and head capture, plus identical
                        # geometry for every probe. Retain every raw image.
                        if view.startswith("updates") and width == 390:
                            for repeat in range(1, 8):
                                probe_context = browser.new_context(
                                    viewport={"width": width, "height": 900},
                                    color_scheme=scheme,
                                    reduced_motion="reduce",
                                    device_scale_factor=1,
                                )
                                probe_context.add_init_script(INIT_SCRIPT)
                                probe_page = probe_context.new_page()
                                probe_errors = []
                                probe_page.on(
                                    "pageerror",
                                    lambda error: probe_errors.append(str(error)),
                                )
                                probe_page.route("**/*", route)
                                probe_page.goto(url)
                                probe_page.wait_for_selector("main")
                                probe_page.evaluate("document.fonts.ready")
                                probe_page.wait_for_timeout(100)
                                assert probe_page.evaluate(GEOMETRY) == geometry, (
                                    key,
                                    "repeatability geometry changed",
                                )
                                name = f"{key}-repeat-{repeat}.png"
                                probe_page.screenshot(
                                    path=str(output / name),
                                    full_page=True,
                                    animations="disabled",
                                )
                                assert not probe_errors and not probe_page.evaluate(
                                    "window.__fixtureUnexpectedFetches"
                                ), (key, probe_errors)
                                probes.append(name)
                                probe_context.close()
            scheme_checks = (
                check_schemes(browser, server.server_port, records)
                if acceptance
                else []
            )
            behavior_checks = (
                check_behaviors(browser, server.server_port) if acceptance else []
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    result = {
        "revision": label,
        "build": build,
        "fixture_only": True,
        "viewport_height": 900,
        "chromium_args": ["--disable-gpu"],
        "captures": records,
        "scheme_checks": scheme_checks,
        "behavior_checks": behavior_checks,
    }
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def check_schemes(browser, port, records):
    results = []
    for view in ["post", "overview", "updates"]:
        for system in ["light", "dark"]:
            stored = "dark" if system == "light" else "light"
            context = browser.new_context(
                viewport={"width": 390, "height": 900},
                color_scheme=system,
                reduced_motion="reduce",
            )
            context.add_init_script(
                f"if (!localStorage.getItem('td-theme')) localStorage.setItem('td-theme', '{stored}');"
            )
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(f"http://127.0.0.1:{port}/")
                    else route.abort()
                ),
            )
            page.goto(f"http://127.0.0.1:{port}/sheets.html?view={view}")
            page.wait_for_selector("main")
            page.wait_for_timeout(100)
            expected = next(
                row for row in records if row["key"] == f"{view}-390-{stored}"
            )
            ground = next(
                item
                for item in expected["opening"]["geometry"]["elements"]
                if item["selector"] == "main"
            )["style"]["backgroundColor"]
            assert page.evaluate("document.documentElement.dataset.theme") == stored, (
                view,
                system,
                stored,
            )
            assert (
                page.locator("main").evaluate(
                    "node => getComputedStyle(node).backgroundColor"
                )
                == ground
            ), (view, system, stored)
            page.emulate_media(color_scheme=stored)
            page.emulate_media(color_scheme=system)
            assert (
                page.locator("main").evaluate(
                    "node => getComputedStyle(node).backgroundColor"
                )
                == ground
            ), view
            if view != "updates":
                button = page.locator("button.scheme")
                assert button.get_attribute("aria-label") == (
                    "Switch to day scheme"
                    if stored == "dark"
                    else "Switch to night scheme"
                )
                button.click()
                page.reload()
                page.wait_for_selector("main")
                assert (
                    page.evaluate("document.documentElement.dataset.theme") == system
                ), view
            results.append(
                {
                    "view": view,
                    "os": system,
                    "persisted": stored,
                    "opposite_os_wins": True,
                    "reload_checked": view != "updates",
                }
            )
            context.close()
    return results


def check_behaviors(browser, port):
    results = []
    for view in ["activity", "overview"]:
        context = browser.new_context(
            viewport={"width": 390, "height": 900}, reduced_motion="reduce"
        )
        page = context.new_page()
        page.clock.install(time=1791028800000)
        page.route(
            "**/*",
            lambda route: (
                route.continue_()
                if route.request.url.startswith(f"http://127.0.0.1:{port}/")
                else route.abort()
            ),
        )
        page.goto(f"http://127.0.0.1:{port}/sheets.html?view={view}")
        page.wait_for_selector("main")
        if view == "activity":
            page.get_by_role("searchbox", name="Search tasks").fill(
                "not-a-synthetic-task"
            )
            assert "nothing matches" in page.locator("main").inner_text(), page.locator(
                "main"
            ).inner_text()
            page.get_by_role("searchbox", name="Search tasks").fill("")
        page.clock.fast_forward(61_000)
        assert not page.evaluate("window.__fixtureUnexpectedFetches"), view
        if view == "activity":
            assert page.evaluate("window.__fixturePolls") > 0, view
        page.evaluate("window.__fixtureUnmount()")
        page.wait_for_timeout(20)
        count = page.evaluate("window.__fixturePolls")
        page.clock.fast_forward(61_000)
        assert page.evaluate("window.__fixturePolls") == count, view
        results.append({"view": view, "poll_and_cleanup": True})
        context.close()
    return results


def compare(before, after, output, *, acceptance=False):
    from PIL import Image, ImageChops

    base = json.loads((before / "results.json").read_text())
    head = json.loads((after / "results.json").read_text())
    assert base["build"]["harness_sha"] == head["build"]["harness_sha"], (
        "Both source revisions must use the same harness"
    )
    differences = []
    regressions = []
    for old, new in zip(base["captures"], head["captures"], strict=True):
        assert old["key"] == new["key"]
        key = old["key"]
        left, right = (
            Image.open(before / f"{key}.png").convert("RGB"),
            Image.open(after / f"{key}.png").convert("RGB"),
        )
        size = (max(left.width, right.width), max(left.height, right.height))
        a, b = Image.new("RGB", size), Image.new("RGB", size)
        a.paste(left)
        b.paste(right)
        delta = ImageChops.difference(a, b)
        bbox = delta.getbbox()
        # A formerly overflowing baseline can have extra off-viewport columns.
        # Compare every pixel within the common visible page, not those columns.
        common = ImageChops.difference(
            left.crop(
                (0, 0, min(left.width, right.width), min(left.height, right.height))
            ),
            right.crop(
                (0, 0, min(left.width, right.width), min(left.height, right.height))
            ),
        ).getbbox()
        match = None
        if common is not None:
            for old_name in old.get("repeatability_images", [f"{key}.png"]):
                old_image = Image.open(before / old_name).convert("RGB")
                for new_name in new.get("repeatability_images", [f"{key}.png"]):
                    new_image = Image.open(after / new_name).convert("RGB")
                    if (
                        old_image.size == new_image.size
                        and ImageChops.difference(old_image, new_image).getbbox()
                        is None
                    ):
                        match = {"base": old_name, "head": new_name}
                        break
                if match:
                    break
        if acceptance:
            if common is not None and match is None:
                regressions.append([key, "visible pixel regression", common])
            if left.height != right.height:
                regressions.append(
                    [key, "document height changed", left.height, right.height]
                )
            for phase in ["opening", "scrolled"]:
                old_geometry = (
                    old[phase]["geometry"] if phase == "opening" else old[phase]
                )
                new_geometry = (
                    new[phase]["geometry"] if phase == "opening" else new[phase]
                )
                for selector in ["body", "main", ".spine", ".toc"]:
                    old_elements = [
                        item
                        for item in old_geometry["elements"]
                        if item["selector"] == selector
                    ]
                    new_elements = [
                        item
                        for item in new_geometry["elements"]
                        if item["selector"] == selector
                    ]
                    for old_item, new_item in zip(
                        old_elements, new_elements, strict=True
                    ):
                        for property in ["overflowX", "overflowY", "position", "top"]:
                            if (
                                old_item["style"][property]
                                != new_item["style"][property]
                            ):
                                regressions.append(
                                    [
                                        key,
                                        phase,
                                        selector,
                                        property,
                                        old_item["style"][property],
                                        new_item["style"][property],
                                    ]
                                )
        if (
            bbox
            or old["opening"] != new["opening"]
            or old["scrolled"] != new["scrolled"]
        ):
            delta.save(output / f"{key}-diff.png")
            differences.append(
                {
                    "key": key,
                    "pixels": bbox,
                    "visible_pixels": common,
                    "exact_repeatability_match": match,
                    "geometry_changed": old["opening"] != new["opening"]
                    or old["scrolled"] != new["scrolled"],
                }
            )
    result = {
        "base": base["revision"],
        "head": head["revision"],
        "compared": len(head["captures"]),
        "differences": differences,
        "regressions": regressions,
    }
    (output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    assert not regressions, regressions
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", default="working-tree")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    args = parser.parse_args()
    if args.artifact:
        capture(args.artifact, args.output, args.label, acceptance=args.acceptance)
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        compare(args.before, args.after, args.output, acceptance=args.acceptance)
