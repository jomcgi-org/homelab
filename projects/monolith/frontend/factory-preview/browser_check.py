"""Chromium-only synthetic QA. Run in CI, never against a live service."""

import argparse
import functools
import hashlib
import http.server
import importlib.metadata
import itertools
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PREFIX = "/synthetic/factory/"
NOW = "2026-10-03T12:00:00.000Z"
VIEWPORTS = [(320, 740), (360, 800), (390, 844), (430, 932), (1440, 1000)]
VIEWS = ("overview", "activity", "context", "chapter", "search")


def digest_tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if not path.startswith(PREFIX):
            self.send_error(403)
            return
        self.path = "/" + path.removeprefix(PREFIX)
        super().do_GET()

    def log_message(self, *_args):
        pass


def matrix():
    for (width, height), scheme in itertools.product(VIEWPORTS, ("light", "dark")):
        for scenario in ("live", "empty", "error"):
            for view in VIEWS if scenario == "live" else VIEWS[:3]:
                yield width, height, scheme, scenario, view, 1
        for view in VIEWS:
            yield width, height, scheme, "live", view, 2
    # The overview switches from document flow to a constrained desktop frame
    # at 901px. Exercise both edges rather than only a generous desktop width.
    for width, scheme, scenario in itertools.product(
        (901, 1024), ("light", "dark"), ("live", "error")
    ):
        yield width, 900, scheme, scenario, "overview", 1


def resize_text(page):
    # Text-only resize: snapshot computed pixels before applying, then double
    # every font and explicit line-height. No transform, zoom, viewport trick,
    # root-rem spacing change, or product stylesheet override hides overflow.
    return page.evaluate("""async () => {
      const rows = [...document.querySelectorAll('.factory-page, .factory-page *')]
        .map(node => ({node, size: parseFloat(getComputedStyle(node).fontSize),
          line: getComputedStyle(node).lineHeight}))
        .filter(({size}) => Number.isFinite(size));
      for (const {node, size, line} of rows) {
        node.style.setProperty('font-size', `${size * 2}px`, 'important');
        if (line !== 'normal') node.style.setProperty('line-height', `${parseFloat(line) * 2}px`, 'important');
      }
      // global.css gives every element a 0.01ms reduced-motion transition.
      // Same-turn getComputedStyle still sees its starting font size. Wait
      // for that real browser transition before asserting computed doubling.
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const mismatches = rows.filter(({node, size}) => node.isConnected &&
        !(Math.abs(parseFloat(getComputedStyle(node).fontSize) - size * 2) < 0.1))
        .map(({node, size}) => ({tag: node.tagName,
          className: node.getAttribute('class'), text: node.textContent.slice(0, 60),
          expected: size * 2, actual: getComputedStyle(node).fontSize}));
      return {count: rows.length, verified: mismatches.length === 0, mismatches};
    }""")


def layout_checks(page, view, scenario, width, scale):
    issues = []

    def require(condition, reason):
        if not condition:
            issues.append(reason)

    overflow = page.evaluate("""() => {
      const root = document.documentElement;
      const factory = document.querySelector('.factory-page');
      return {page: root.scrollWidth - innerWidth,
        factory: factory.scrollWidth - factory.clientWidth};
    }""")
    require(overflow["page"] <= 1, f"horizontal page overflow: {overflow}")
    require(overflow["factory"] <= 1, f"horizontal factory overflow: {overflow}")

    targets = page.locator(
        ".view-tabs a, .pager button, .mast-actions .scheme"
    ).evaluate_all("""nodes => nodes.map(node => {
      const box = node.getBoundingClientRect();
      return {text: node.textContent.trim() || node.getAttribute('aria-label'), width: box.width, height: box.height};
    })""")
    for target in targets:
        if width <= 430:
            require(
                target["width"] >= 43.9 and target["height"] >= 43.9,
                f"mobile target below 44px: {target}",
            )

    if width <= 430 and scenario == "live":
        titles = page.locator(
            ".lane-list .t, .prs li:not(.hd) a, .activity-page .rows .t, .doc summary > span, .results li > span:nth-child(2)"
        )
        for title in titles.evaluate_all("""nodes => nodes.map(node => {
          const style = getComputedStyle(node), box = node.getBoundingClientRect();
          return {text: node.textContent.slice(0, 70), width: box.width,
            height: box.height, whiteSpace: style.whiteSpace, overflow: style.textOverflow,
            excess: node.scrollWidth - node.clientWidth,
            fontSize: parseFloat(style.fontSize)};
        })"""):
            require(
                title["width"] > 0 and title["height"] > 0, f"hidden title: {title}"
            )
            require(
                title["whiteSpace"] != "nowrap" and title["overflow"] != "ellipsis",
                f"mobile title is truncated: {title}",
            )
            # Inline spans have a zero clientWidth, so page containment handles
            # those. Block/grid title cells must contain the unbroken token.
            require(title["excess"] <= 1, f"title overflows its cell: {title}")
            require(title["fontSize"] >= 13 * scale, f"mobile title too small: {title}")

    if view == "overview":
        mobile = page.locator("details.mobile-stats")
        desktop = page.locator(".home > .stats")
        if width <= 900:
            require(mobile.is_visible(), "mobile live/merged summary missing")
            require(
                mobile.get_attribute("open") is None,
                "mobile totals expanded by default",
            )
            require(
                not desktop.is_visible(), "six desktop stat boxes still shown on mobile"
            )
            require(
                not any(
                    spark.is_visible() for spark in page.locator(".home .sp").all()
                ),
                "tiny sparklines still visible on mobile",
            )
            summary = " ".join(mobile.locator("summary").inner_text().split())
            expected = (
                ("Live unavailable", "Merges unavailable")
                if scenario == "error"
                else (
                    f"{2 if scenario == 'live' else 0} live",
                    f"{24 if scenario == 'live' else 0} merged this week",
                )
            )
            require(
                all(value in summary for value in expected),
                f"incorrect live/merged summary: {summary}",
            )
            box = mobile.locator("summary").bounding_box()
            require(bool(box and box["height"] >= 43.9), "All stats target below 44px")
        else:
            require(not mobile.is_visible(), "mobile summary shown on desktop")
            require(
                desktop.is_visible() and desktop.locator(":scope > div").count() == 6,
                "desktop lost six headline totals",
            )
        for legend in page.locator(".charts .legend span").all():
            require(
                legend.is_visible(), f"hidden chart legend: {legend.text_content()}"
            )
        if scenario != "error":
            charts = page.locator(".charts .chart > svg").all()
            require(len(charts) == 3, "expected three production charts")
            for chart in charts:
                box = chart.bounding_box()
                if width <= 430:
                    require(
                        bool(box and box["height"] >= 119),
                        f"mobile chart too short to read: {box}",
                    )
                labels = chart.locator("text").evaluate_all("""nodes => nodes
                  .filter(node => /^\\d{2}·\\d{2}$/.test(node.textContent))
                  .map(node => { const box = node.getBoundingClientRect();
                    return {text: node.textContent, left: box.left, right: box.right};
                  }).sort((a, b) => a.left - b.left)""")
                for previous, current in itertools.pairwise(labels):
                    require(
                        previous["right"] <= current["left"] + 0.1,
                        f"chart dates overlap: {previous} and {current}",
                    )
        if scenario == "live":
            current = page.locator(".lane-list li").first
            require(current.is_visible(), "current task missing")
            require(
                "review" in current.inner_text() and "47m ago" in current.inner_text(),
                "current task phase or elapsed time missing",
            )
            require(current.locator(".m").is_visible(), "current task metadata hidden")
        elif scenario == "empty":
            require(
                page.get_by_text("Nothing in the lane.", exact=True).is_visible(),
                "empty lane missing",
            )
    elif view == "activity" and scenario == "live":
        current = page.locator('.rows a[aria-label="Open task 900001"]')
        require(current.is_visible(), "current activity task missing")
        require(
            "review" in current.inner_text() and "47m ago" in current.inner_text(),
            "activity phase or elapsed time hidden",
        )
    if scenario == "error":
        require(
            page.locator(".unavailable").is_visible(),
            "error is not distinguished from empty",
        )

    animated = page.locator(".factory-page *").evaluate_all("""nodes => nodes.filter(node => {
      const style = getComputedStyle(node);
      return style.animationName !== 'none' && style.animationDuration.split(',').some(value => parseFloat(value) > 0.001);
    }).map(node => node.className)""")
    require(not animated, f"motion still running under reduced motion: {animated}")
    return issues, {"overflow": overflow, "targets": targets}


def check_mobile_totals(page, scenario, expanded_screenshot=None):
    details = page.locator("details.mobile-stats")
    summary = details.locator("summary")
    summary.focus()
    page.keyboard.press("Enter")
    page.locator(".mobile-stat-values").wait_for(state="visible")
    assert details.get_attribute("open") is not None, "keyboard cannot expand All stats"
    rows = page.locator(".mobile-stat-values > div").evaluate_all("""nodes => nodes.map(node => ({
      label: node.querySelector('dt').textContent.trim(), value: node.querySelector('dd').textContent.trim()
    }))""")
    labels = [
        "Live",
        "Sessions, 7d",
        "Merged, 7d",
        "Tokens, 7d",
        "Spend, 7d (list)",
        "Facts",
    ]
    values = (
        ["2", "164", "24", "1.5M", "$38", "26"]
        if scenario == "live"
        else ["unavailable"] * 6
        if scenario == "error"
        else ["0", "0", "0", "0", "$0", "0"]
    )
    assert rows == [
        {"label": label, "value": value} for label, value in zip(labels, values)
    ], f"All stats lost or changed totals: {rows}"
    assert not any(spark.is_visible() for spark in page.locator(".home .sp").all()), (
        "expanded totals restored tiny sparklines"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
        "expanded totals introduce page overflow"
    )
    if expanded_screenshot:
        page.screenshot(path=str(expanded_screenshot), full_page=True)
    summary.focus()
    page.keyboard.press("Space")
    page.locator(".mobile-stat-values").wait_for(state="hidden")
    assert details.get_attribute("open") is None, "keyboard cannot collapse All stats"


def interact(page, view):
    nav = page.get_by_role("navigation", name="Factory views")
    links = nav.get_by_role("link")
    links.nth(0).focus()
    page.keyboard.press("Tab")
    assert links.nth(1).evaluate("node => node === document.activeElement"), (
        "keyboard tab skips activity"
    )
    assert links.nth(1).evaluate(
        "node => getComputedStyle(node).outlineStyle !== 'none'"
    ), "focus is not visible"
    page.keyboard.press("Shift+Tab")
    assert links.nth(0).evaluate("node => node === document.activeElement")

    toggle = page.get_by_role("button", name=re.compile("Switch to .* scheme"))
    before = toggle.get_attribute("aria-label")
    toggle.focus()
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-label") != before, (
        "scheme switch did not respond to keyboard"
    )
    page.keyboard.press("Enter")

    next_button = page.locator(".pager button").filter(has_text="next").first
    if next_button.count() and next_button.is_enabled():
        before = page.locator(".pager").first.inner_text()
        next_button.focus()
        page.keyboard.press("Enter")
        page.wait_for_function(
            "before => document.querySelector('.pager').innerText !== before",
            arg=before,
        )
        prev = page.locator(".pager button").filter(has_text="prev").first
        assert prev.is_enabled(), "pager did not advance"
        prev.focus()
        page.keyboard.press("Enter")

    if view == "activity":
        search = page.get_by_role("searchbox", name="Search tasks")
        search.fill("no-synthetic-task-can-match-this")
        page.get_by_text("nothing matches", exact=True).first.wait_for()
        search.fill("")
        page.get_by_role("link", name="Open task 900001", exact=True).wait_for()
    if view == "chapter":
        summary = page.locator(".doc details > summary").first
        summary.focus()
        page.keyboard.press("Enter")
        assert summary.locator("..").get_attribute("open") is not None, (
            "keyboard cannot expand context record"
        )
        page.keyboard.press("Enter")
    if view in ("context", "chapter", "search"):
        search = page.get_by_role("combobox", name="Search the record")
        search.fill("Synthetic")
        page.get_by_role("listbox").wait_for()
        search.press("ArrowDown")
        assert (
            search.get_attribute("aria-activedescendant") == "factory-search-option-0"
        )
        search.press("Escape")
        assert not page.get_by_role("listbox").count(), (
            "Escape did not close instant search"
        )
        search.fill("")

    # The controls above are unmodified production controls. Only routing to
    # another synthetic page is handled by the bounded static harness.
    nav.get_by_role("link", name="activity", exact=True).click()
    page.locator(".activity-page").wait_for()
    nav.get_by_role("link", name="context", exact=True).click()
    page.locator(".context-page").wait_for()
    page.go_back(wait_until="domcontentloaded")
    page.locator(".activity-page").wait_for()


def check(root, output, expected_sha):
    output.mkdir(parents=True, exist_ok=True)
    before = digest_tree(root)
    build = json.loads((root / "build.json").read_text())
    assert build["commit"] == expected_sha, "artifact is not from the requested commit"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}{PREFIX}"
    evidence = {
        "commit": expected_sha,
        "playwright": importlib.metadata.version("playwright"),
        "cases": [],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            evidence["chromium"] = browser.version
            for width, height, scheme, scenario, view, scale in matrix():
                name = f"{view}-{scenario}-{width}x{height}-{scheme}-{scale * 100}pct"
                record = {
                    "name": name,
                    "view": view,
                    "scenario": scenario,
                    "width": width,
                    "scheme": scheme,
                    "text_scale": scale,
                    "issues": [],
                }
                evidence["cases"].append(record)
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    color_scheme=scheme,
                    reduced_motion="reduce",
                    service_workers="block",
                )
                context.add_init_script("""
                    window.__fixtureCspViolations = [];
                    addEventListener('securitypolicyviolation', event => window.__fixtureCspViolations.push(event.violatedDirective + ':' + event.blockedURI));
                """)
                page = context.new_page()
                page.set_default_timeout(5000)
                page.clock.set_fixed_time(NOW)
                forbidden, errors, failures = [], [], []

                def guard(route, forbidden=forbidden):
                    if not route.request.url.startswith(base):
                        forbidden.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", guard)
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                page.on(
                    "requestfailed",
                    lambda request, failures=failures: failures.append(request.url),
                )
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                try:
                    page.goto(
                        base + "?" + urlencode({"view": view, "scenario": scenario}),
                        wait_until="load",
                    )
                    page.wait_for_function("window.__factoryFixtureReady === true")
                    page.evaluate("document.fonts.ready")
                    assert page.evaluate(
                        "document.fonts.check('16px \"Schibsted Grotesk\"')"
                    ), "local production font failed to load"
                    if scale == 2:
                        record["text_resize"] = resize_text(page)
                        if not record["text_resize"]["verified"]:
                            record["issues"].append(
                                f"computed text did not double: {record['text_resize']['mismatches'][:3]}"
                            )
                    page.evaluate(
                        "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                    )
                    issues, metrics = layout_checks(page, view, scenario, width, scale)
                    record["issues"].extend(issues)
                    record.update(metrics)
                    if (
                        record["issues"]
                        or (scenario == "live" and width in (390, 901, 1440))
                        or (
                            scenario == "live"
                            and view == "overview"
                            and width == 320
                            and scale == 2
                        )
                    ):
                        page.screenshot(
                            path=str(output / f"{name}-full.png"), full_page=True
                        )
                    if scenario == "live" and scale == 1 and width in (390, 901, 1440):
                        page.screenshot(
                            path=str(output / f"{name}-opening.png"), full_page=False
                        )
                    if view == "overview" and width <= 900:
                        check_mobile_totals(
                            page,
                            scenario,
                            output / f"{name}-expanded.png"
                            if width == 320 and scale == 2 and scenario == "live"
                            else None,
                        )
                    if scenario == "live" and scale == 1:
                        interact(page, view)
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 1"
                        ), "interaction introduced page overflow"
                    assert not page.evaluate("window.__fixtureUnexpectedFetches"), (
                        "unexpected fetch escaped fixture mapping"
                    )
                    assert not page.evaluate("window.__fixtureCspViolations"), (
                        "CSP blocked a request"
                    )
                    assert not forbidden, f"external request: {forbidden}"
                    assert not failures, f"asset request failed: {failures}"
                    assert not errors, f"browser error: {errors}"
                except (AssertionError, PlaywrightError) as error:
                    record["issues"].append(str(error))
                    page.screenshot(
                        path=str(output / f"{name}-failure.png"), full_page=True
                    )
                finally:
                    record["status"] = "failed" if record["issues"] else "passed"
                    # The successful screenshots are enough for visual review;
                    # traces are retained only when an assertion failed.
                    trace = output / f"{name}-trace.zip" if record["issues"] else None
                    if trace:
                        context.tracing.stop(path=str(trace))
                    else:
                        context.tracing.stop()
                    context.close()
                    print(f"{record['status']}: {name}", flush=True)
                    for issue in record["issues"]:
                        print(f"  {issue}", flush=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        (output / "tested-files.json").write_text(json.dumps(before, indent=2) + "\n")
    assert before == digest_tree(root), "browser check changed the static artifact"
    failed = [case for case in evidence["cases"] if case["status"] != "passed"]
    expected_cases = len(list(matrix()))
    assert len(evidence["cases"]) == expected_cases, "browser matrix was incomplete"
    assert not failed, (
        f"{len(failed)} of {expected_cases} browser cases failed; see result.json and screenshots"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.expected_sha)
