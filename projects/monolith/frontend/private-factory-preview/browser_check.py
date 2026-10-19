"""Real Chromium, production components and synthetic responses only."""

import argparse
import functools
import hashlib
import http.server
import importlib.metadata
import itertools
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

PREFIX = "/synthetic/private-factory/"
ROUTES = {
    "board": "/factory",
    "escalations": "/factory/escalations",
    "item": "/factory/work-items/100001",
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


def measurements(page):
    return page.evaluate("""() => {
      const main = document.querySelector('main.td.sheet');
      const content = main.querySelector('.content');
      const spine = main.querySelector('.spine');
      const controls = [...main.querySelectorAll('button,input,select,textarea')]
        .filter(el => el.checkVisibility()).map(el => {
          const box = el.getBoundingClientRect(), css = getComputedStyle(el);
          return {tag: el.tagName, label: el.getAttribute('aria-label') || el.textContent.trim().slice(0,70),
            width: box.width, height: box.height, left: box.left, right: box.right,
            color: css.color, background: css.backgroundColor, radius: css.borderRadius,
            shadow: css.boxShadow, disabled: el.disabled};
        });
      return {viewport: {width: innerWidth, height: innerHeight},
        pageOverflow: Math.max(document.body.scrollWidth,document.documentElement.scrollWidth)-innerWidth,
        mainOverflow: main.scrollWidth-main.clientWidth,
        contentOverflow: content.scrollWidth-content.clientWidth,
        bodyOverflowX: getComputedStyle(document.body).overflowX,
        bodyOverflowY: getComputedStyle(document.body).overflowY,
        background: getComputedStyle(main).backgroundColor,
        color: getComputedStyle(main).color,
        scheme: document.documentElement.dataset.theme,
        chromeCount: document.querySelectorAll('.chrome').length,
        legacyBackCount: document.querySelectorAll('.back-to-dashboard').length,
        sticky: {position:getComputedStyle(spine).position, top:getComputedStyle(spine).top},
        controls,
        dialogs: [...document.querySelectorAll('dialog,[role="dialog"]')].map(el=>({open:el.open,visible:el.checkVisibility()})),
        confirmation: document.querySelector('.confirm')?.textContent ?? null};
    }""")


def capture(page, output, records, commit, view, scenario, scheme, state="base"):
    page.evaluate("document.fonts.ready")
    data = measurements(page)
    name = f"{view}-{scenario}-{data['viewport']['width']}-{scheme}-{state}"
    page.screenshot(path=str(output / f"{name}.png"), full_page=True)
    row = {
        "commit": commit,
        "route": ROUTES[view],
        "scenario": scenario,
        "state": state,
        "scheme": scheme,
        "image": f"{name}.png",
        **data,
    }
    records.append(row)
    assert data["pageOverflow"] == 0, f"{name}: page horizontal overflow {data}"
    assert data["mainOverflow"] == 0, f"{name}: main overflow {data}"
    assert data["contentOverflow"] == 0, f"{name}: content overflow {data}"
    assert data["bodyOverflowY"] == "auto", f"{name}: document scroll contract {data}"
    assert data["chromeCount"] == 1 and data["legacyBackCount"] == 0, (
        f"{name}: duplicate chrome {data}"
    )
    assert data["sticky"]["position"] == "sticky", f"{name}: lost sticky index {data}"
    assert data["scheme"] == scheme
    for control in data["controls"]:
        assert control["radius"] == "0px" and control["shadow"] == "none", (
            f"{name}: legacy control style {control}"
        )
        assert (
            control["left"] >= -1 and control["right"] <= data["viewport"]["width"] + 1
        ), f"{name}: offscreen control {control}"
        assert control["height"] >= 43, f"{name}: short control {control}"


def actions(page, view, snapshot):
    if view == "board":
        button = page.locator(".panel.admitted .panel-head")
        button.click()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".panel.admitted .detail")).to_be_visible()
        assert page.evaluate(
            "window.__privateFixture.navigation.at(-1).url"
        ).startswith("/factory?task=t-synthetic-")
        snapshot("expanded")
        button.click()
        expect(button).to_have_attribute("aria-expanded", "false")
        assert page.evaluate(
            "window.__privateFixture.navigation.at(-1).options.replaceState"
        )
    elif view == "escalations":
        page.locator("textarea").fill("Synthetic note pinned to receipt 31")
        page.locator("textarea").press("Tab")
        page.keyboard.press("e")
        expect(page.locator(".context details").first).to_have_attribute("open", "")
        page.locator(".escape-btn").first.click()
        expect(page.locator(".confirm")).to_contain_text("90031")
        assert not page.evaluate(
            "window.__privateFixture.requests.some(r=>r.method==='POST')"
        )
        snapshot("confirmation")
        page.evaluate("window.__privateFixture.escalations.reverse()")
        page.clock.fast_forward(20000)
        expect(page.locator(".panel").nth(1).locator(".confirm")).to_be_visible()
        page.keyboard.press("x")
        page.wait_for_function(
            "window.__privateFixture.requests.some(r=>r.method==='POST')"
        )
        expect(page.locator('[aria-label="Open escalations"]')).to_have_attribute(
            "aria-busy", "false"
        )
        request = page.evaluate(
            "window.__privateFixture.requests.find(r=>r.method==='POST')"
        )
        assert (
            request["url"].endswith("/31")
            and request["body"]["note"] == "Synthetic note pinned to receipt 31"
        )
        assert request["body"]["option_key"] == "escape:close"
        # A transport error and uncertain outcome replay the key. A definitive
        # refusal consumes it, and the next attempt receives a new key.
        page.evaluate(
            "window.__privateFixture.outcomes=['transport','unknown','refused','accepted']"
        )
        for outcome in (
            "could not be sent",
            "outcome unknown",
            "definitive refusal",
            None,
        ):
            page.locator(".option").first.click()
            expect(page.locator('[aria-label="Open escalations"]')).to_have_attribute(
                "aria-busy", "false"
            )
            if outcome:
                expect(page.locator('[role="alert"]')).to_contain_text(outcome)
        keys = page.evaluate(
            "window.__privateFixture.requests.filter(r=>r.method==='POST').slice(-4).map(r=>r.body.request_key)"
        )
        assert keys[0] == keys[1] == keys[2] and keys[3] != keys[2], keys
        page.evaluate("window.__privateFixture.hold=true")
        page.locator(".option").first.click()
        expect(page.locator('[aria-label="Open escalations"]')).to_have_attribute(
            "aria-busy", "true"
        )
        expect(page.locator(".option").first).to_be_disabled()
        page.wait_for_function("typeof window.__privateFixture.release === 'function'")
        page.evaluate("window.__privateFixture.release()")
        expect(page.locator('[aria-label="Open escalations"]')).to_have_attribute(
            "aria-busy", "false"
        )
    else:
        page.locator(".event-summary").click()
        page.locator('input[name="other"]').fill("#90099")
        page.locator('select[name="direction"]').select_option("parent|in")
        snapshot("relationships-expanded")
        page.evaluate("window.__privateFixture.outcomes=['refused']")
        page.locator(".submit-btn").click()
        expect(page.locator('[role="alert"]')).to_contain_text("definitive refusal")
        page.locator(".submit-btn").click()
        expect(page.locator('input[name="other"]')).to_have_value("")
        posts = page.evaluate(
            "window.__privateFixture.requests.filter(r=>r.method==='POST')"
        )
        assert posts[-1]["body"] == {
            "kind": "parent",
            "direction": "in",
            "other": "#90099",
            "stated_reason": None,
        }
        page.locator(".remove-btn").first.click()
        expect(page.locator(".remove-btn")).to_have_count(5)
        assert page.evaluate(
            "window.__privateFixture.requests.some(r=>r.method==='DELETE' && r.url.endsWith('/edges/1'))"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    build = json.loads((artifact / "build.json").read_text())
    assert build["commit"] == args.expected_sha, (
        "fixture SHA does not match tested head"
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(artifact))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    records, failures, checks = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for view, width, scheme in itertools.product(
            ROUTES, (390, 1440), ("light", "dark")
        ):
            for scenario in (
                "populated",
                "empty",
                "unavailable",
                *(("missing",) if view == "item" else ()),
            ):
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    color_scheme="dark" if scheme == "light" else "light",
                    reduced_motion="reduce",
                    service_workers="block",
                )
                context.add_init_script(
                    f"localStorage.setItem('td-theme', '{scheme}');"
                )
                page = context.new_page()
                page.set_default_timeout(5000)
                errors, network = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def intercept(route):
                    if route.request.url.startswith(origin + PREFIX):
                        route.continue_()
                    else:
                        network.append(route.request.url)
                        route.abort()

                context.route("**/*", intercept)
                context.tracing.start(screenshots=True, snapshots=True)
                name = f"{view}-{scenario}-{width}-{scheme}"
                try:
                    page.clock.install(
                        time=datetime(2026, 10, 19, 9, 1, tzinfo=timezone.utc)
                    )
                    page.goto(
                        origin
                        + PREFIX
                        + "?"
                        + urlencode({"view": view, "scenario": scenario})
                    )
                    page.wait_for_function("window.__privateFixture?.ready")
                    page.evaluate("document.fonts.ready")
                    expect(page.locator("main.td.sheet")).to_be_visible()
                    snapshot = lambda state="base": capture(
                        page,
                        args.output,
                        records,
                        args.expected_sha,
                        view,
                        scenario,
                        scheme,
                        state,
                    )
                    snapshot()
                    if scenario == "populated":
                        actions(page, view, snapshot)
                        # A real scroll must retain the mobile index at the top.
                        if width == 390:
                            page.evaluate("window.scrollTo(0, 400)")
                            top = page.locator(".spine").evaluate(
                                "el=>el.getBoundingClientRect().top"
                            )
                            assert abs(top) <= 1, f"sticky private index top={top}"
                        page.locator(".scheme").click()
                        opposite = "dark" if scheme == "light" else "light"
                        expect(page.locator("html")).to_have_attribute(
                            "data-theme", opposite
                        )
                        assert (
                            page.evaluate("localStorage.getItem('td-theme')")
                            == opposite
                        )
                    assert not errors, errors
                    assert not network, network
                    assert not page.evaluate("window.__privateFixture.unexpected"), (
                        "unexpected fixture fetch"
                    )
                    checks.append(
                        {
                            "case": name,
                            "passed": True,
                            "requests": page.evaluate(
                                "window.__privateFixture.requests"
                            ),
                        }
                    )
                    context.tracing.stop()
                except Exception as error:
                    failures.append(
                        {
                            "case": name,
                            "error": str(error),
                            "pageErrors": errors,
                            "unexpectedNetwork": network,
                        }
                    )
                    page.screenshot(
                        path=str(args.output / f"{name}-failure.png"), full_page=True
                    )
                    context.tracing.stop(path=str(args.output / f"{name}-trace.zip"))
                finally:
                    context.close()
        version = browser.version
        browser.close()
    server.shutdown()
    digests = {
        str(path.relative_to(artifact)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(artifact.rglob("*"))
        if path.is_file()
    }
    result = {
        "commit": args.expected_sha,
        "build": build,
        "chromium": version,
        "playwright": importlib.metadata.version("playwright"),
        "records": records,
        "checks": checks,
        "failures": failures,
        "artifact_sha256": digests,
    }
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        f"# Private factory evidence\n\nTested head: `{args.expected_sha}`\n",
        "[Machine-readable overflow, provenance and action checks](results.json)\n",
        "| Route | Scenario/state | Width | Scheme | Page overflow | Screenshot |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        lines.append(
            f"| {row['route']} | {row['scenario']}/{row['state']} | {row['viewport']['width']} | {row['scheme']} | {row['pageOverflow']} | [{row['image']}]({row['image']}) |"
        )
    (args.output / "MATRIX.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "commit": args.expected_sha,
                "screenshots": len(records),
                "passed": len(checks),
                "failures": failures,
            },
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
