"""Dashboard and adjacent cascade checks, with no production network or actions."""

import argparse
import functools
import hashlib
import http.server
import itertools
import json
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright

from browser_check import Handler, PREFIX


def measure(page):
    return page.evaluate("""() => {
      const main = document.querySelector('main, .drain-page');
      const css = getComputedStyle(main);
      return {
        width: innerWidth, documentWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
        mainWidth: main.clientWidth, mainScrollWidth: main.scrollWidth,
        documentHeight: document.documentElement.scrollHeight,
        bodyOverflow: getComputedStyle(document.body).overflowY,
        mainOverflow: css.overflowY, background: css.backgroundColor,
        ink: css.getPropertyValue('--ink').trim(), sheet: css.getPropertyValue('--sheet').trim(),
        scheme: document.documentElement.dataset.theme,
        backLinks: document.querySelectorAll('.back-to-dashboard').length,
        backInk: document.querySelector('.back-to-dashboard') ? getComputedStyle(document.querySelector('.back-to-dashboard')).getPropertyValue('--ink').trim() : null,
        schemeControls: document.querySelectorAll('.scheme').length,
        containers: [...main.querySelectorAll('.grid,.launcher-grid,.edition,.chat-log')].map(el => ({
          name: el.className, client: el.clientWidth, scroll: el.scrollWidth
        }))
      };
    }""")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    artifact = args.artifact.resolve()
    build = json.loads((artifact / "build.json").read_text())
    assert build["commit"] == args.expected_sha
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(artifact))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    records, failures = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width, scheme, scenario in itertools.product(
            (390, 1440), ("light", "dark"), ("populated", "empty", "unavailable")
        ):
            context = browser.new_context(
                viewport={"width": width, "height": 900},
                color_scheme="dark" if scheme == "light" else "light",
                reduced_motion="reduce",
                service_workers="block",
            )
            context.add_init_script(f"localStorage.setItem('td-theme', '{scheme}');")
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
            name = f"dashboard-{width}-{scheme}-{scenario}"
            try:
                page.clock.install(
                    time=datetime(2026, 10, 29, 9, 1, tzinfo=timezone.utc)
                )
                page.goto(
                    origin
                    + PREFIX
                    + "?"
                    + urlencode({"view": "dashboard", "scenario": scenario})
                )
                expect(page.locator("main.dashboard")).to_be_visible()
                page.evaluate("document.fonts.ready")

                def capture(label):
                    page.evaluate("window.scrollTo(0, 0)")
                    result = measure(page)
                    result.update(
                        image=f"{name}-{label}.png",
                        state=label,
                        scenario=scenario,
                        commit=args.expected_sha,
                    )
                    page.screenshot(
                        path=str(args.output / result["image"]), full_page=True
                    )
                    records.append(result)
                    assert (
                        result["documentWidth"] <= width
                        and result["bodyWidth"] <= width
                    ), result
                    assert result["mainScrollWidth"] <= result["mainWidth"], result
                    for item in result["containers"]:
                        assert item["scroll"] <= item["client"], item
                    return result

                root = capture("root")
                assert root["scheme"] == scheme and root["bodyOverflow"] == "auto", root
                assert root["backLinks"] == 0 and root["schemeControls"] == 1, root
                page.keyboard.press("Tab")
                focus = page.evaluate(
                    """() => { const el = document.activeElement; const s = getComputedStyle(el); return {tag: el.tagName, width: s.outlineWidth, style: s.outlineStyle}; }"""
                )
                assert focus["style"] != "none" and focus["width"] != "0px", focus
                root["keyboardFocus"] = focus
                page.evaluate(
                    "window.scrollTo(0, document.documentElement.scrollHeight)"
                )
                root["scrollY"] = page.evaluate("window.scrollY")
                assert root["scrollY"] > 0, root
                if scenario == "populated":
                    task = page.get_by_role(
                        "button", name="Toggle Synthetic daily task", exact=True
                    )
                    page.evaluate("window.__privateFixture.hold = true")
                    task.click()
                    expect(task).to_have_attribute("aria-pressed", "true")
                    page.evaluate("window.__privateFixture.release()")
                    page.evaluate(
                        'window.__privateFixture.hold = false; window.__privateFixture.outcomes = ["refused"]'
                    )
                    task.click()
                    expect(task).to_have_attribute("aria-pressed", "true")
                    page.evaluate(
                        "window.__privateFixture.dashboard.health.scanned = 77"
                    )
                    page.clock.fast_forward(60000)
                    expect(page.locator(".pulse")).to_contain_text(
                        "77 workloads scanned"
                    )
                    for failure in ("refused", "error", "transport"):
                        page.evaluate(
                            "(failure) => window.__privateFixture.outcomes = [failure]",
                            failure,
                        )
                        page.clock.fast_forward(60000)
                        expect(page.locator(".pulse")).to_contain_text(
                            "77 workloads scanned"
                        )
                    chat = page.get_by_role("textbox", name="Ask the cluster")
                    chat.fill("Synthetic browser question")
                    chat.press("Enter")
                    expect(page.locator(".msg-text")).to_have_text(
                        "Synthetic reply complete"
                    )
                    chat.fill("Synthetic follow-up")
                    chat.press("Enter")
                    expect(page.locator(".msg-text")).to_have_count(2)
                    history = page.evaluate(
                        'window.__privateFixture.requests.filter(r => r.url === "/dashboard-chat").at(-1).body.history'
                    )
                    assert len(history) == 2, history
                    root["chatHistory"] = history
                    root["taskAndPolling"] = (
                        "optimistic success/refusal; 60s refresh and last-good retention"
                    )
                    capture("interacted")

                    # Same bundle and real layouts, after a sheet has been visited.
                    for child in ("updates", "qwen", "board"):
                        await_navigation = (
                            f'window.__privateFixture.navigate("{child}")'
                        )
                        page.evaluate(await_navigation)
                        expect(page.locator("main")).to_be_visible()
                        child_result = capture(f"client-{child}")
                        if child == "board":
                            assert (
                                child_result["schemeControls"] == 1
                                and child_result["backLinks"] == 0
                            )
                            assert child_result["bodyOverflow"] == "auto"
                        else:
                            assert child_result["schemeControls"] == 0
                            assert child_result["backLinks"] == (
                                1 if child == "updates" else 0
                            )
                            if child == "updates":
                                # The journal sits on the sheet: it follows the
                                # stored scheme, like the dashboard.
                                assert child_result["sheet"] == (
                                    "#ffffff" if scheme == "light" else "#181a20"
                                ), child_result
                                assert child_result["backInk"] == root["ink"], (
                                    child_result
                                )
                                child_result["backClearsContent"] = page.evaluate(
                                    """() => document.querySelector('.back-to-dashboard').getBoundingClientRect().bottom < document.querySelector('.spine').getBoundingClientRect().top"""
                                )
                                assert child_result["backClearsContent"], child_result
                            else:
                                # Qwen still follows the OS, opposite to the stored scheme.
                                assert child_result["sheet"] == (
                                    "#181a20" if scheme == "light" else "#ffffff"
                                ), child_result
                        if child == "qwen":
                            assert child_result["bodyOverflow"] == "hidden", (
                                child_result
                            )
                            # Qwen's mobile chat stays viewport-high, while its
                            # existing stats panel flows below it.
                            child_result["chatHeight"] = page.locator(
                                ".qwen-page .chat"
                            ).evaluate("(el) => el.getBoundingClientRect().height")
                            assert child_result["chatHeight"] == 900, child_result
                            child_result["transcriptOverflow"] = page.locator(
                                ".qwen-page .log"
                            ).evaluate("(el) => getComputedStyle(el).overflowY")
                            assert child_result["transcriptOverflow"] == "auto", (
                                child_result
                            )
                        # Fresh-load parity checks catch stylesheet-order-only fixes.
                        page.goto(
                            origin
                            + PREFIX
                            + "?"
                            + urlencode({"view": child, "scenario": scenario})
                        )
                        expect(page.locator("main")).to_be_visible()
                        fresh = capture(f"fresh-{child}")
                        for field in (
                            "bodyOverflow",
                            "mainOverflow",
                            "sheet",
                            "backLinks",
                            "schemeControls",
                            "backInk",
                        ):
                            assert fresh[field] == child_result[field], (
                                field,
                                fresh,
                                child_result,
                            )
                    page.evaluate('window.__privateFixture.navigate("dashboard")')
                    expect(page.locator("main.dashboard")).to_be_visible()
                    assert measure(page)["scheme"] == scheme
                    assert measure(page)["background"] == root["background"]
                assert not errors and not network, (errors, network)
                unexpected = page.evaluate("window.__privateFixture.unexpected")
                assert not unexpected, unexpected
            except Exception as error:
                failures.append(
                    {
                        "case": name,
                        "error": str(error),
                        "traceback": traceback.format_exc(),
                        "pageErrors": errors,
                        "network": network,
                    }
                )
            finally:
                context.close()
        for width, scheme, view in itertools.product(
            (390, 1440), ("light", "dark"), ("execution", "drain")
        ):
            context = browser.new_context(
                viewport={"width": width, "height": 900},
                color_scheme="dark" if scheme == "light" else "light",
                reduced_motion="reduce",
                service_workers="block",
            )
            context.add_init_script(f"localStorage.setItem('td-theme', '{scheme}');")
            page = context.new_page()
            page.set_default_timeout(5000)
            errors, network = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            context.route("**/*", intercept)
            name = f"dashboard-{width}-{scheme}-{view}"
            try:
                page.clock.install(
                    time=datetime(2026, 10, 29, 9, 1, tzinfo=timezone.utc)
                )
                page.goto(origin + PREFIX + "?" + urlencode({"view": view}))
                target = ".topbar .wordmark" if view == "execution" else ".top .back"
                expect(page.locator(target)).to_be_visible()
                page.evaluate("document.fonts.ready")
                result = measure(page)
                result.update(image=f"{name}.png", state=view, commit=args.expected_sha)
                # The console and drain carry their own header, whose trail root
                # is the dashboard, so the shared back link stays out of their
                # way entirely: nothing may overlap the header's own control.
                bounds = page.evaluate(
                    """(target) => {
                  const own = document.querySelector(target);
                  const h = own.getBoundingClientRect();
                  const console = document.querySelector('main.console');
                  return {
                    backLinks: document.querySelectorAll('.back-to-dashboard').length,
                    headerTop: h.top,
                    headerClickable: own.contains(document.elementFromPoint(h.x + h.width / 2, h.y + h.height / 2)),
                    consoleBottom: console?.getBoundingClientRect().bottom ?? null
                  };
                }""",
                    target,
                )
                result["headerBounds"] = bounds
                assert bounds["backLinks"] == 0, bounds
                assert bounds["headerTop"] >= 0, bounds
                assert bounds["headerClickable"], bounds
                assert result["documentWidth"] == width, result
                if view == "execution":
                    assert bounds["consoleBottom"] <= 900, bounds
                page.screenshot(path=str(args.output / result["image"]), full_page=True)
                records.append(result)
                assert page.evaluate("window.__privateFixture.unexpected") == []
                # Exercise the real links; the fixture routes them without a server.
                page.locator(target).click()
                expect(
                    page.locator(
                        "main.td.sheet" if view == "execution" else "main.console"
                    )
                ).to_be_visible()
                assert not errors and not network, (errors, network)
            except Exception as error:
                failures.append(
                    {
                        "case": name,
                        "error": str(error),
                        "traceback": traceback.format_exc(),
                        "pageErrors": errors,
                        "network": network,
                    }
                )
            finally:
                context.close()
        version = browser.version
        browser.close()
    server.shutdown()
    result = {
        "commit": args.expected_sha,
        "build": build,
        "chromium": version,
        "records": records,
        "failures": failures,
        "asset_sha256": {
            str(p.relative_to(artifact)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in artifact.rglob("*")
            if p.is_file()
        },
    }
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        f"# Dashboard synthetic evidence\n\nHead: `{args.expected_sha}`\n",
        "[Measurements and source provenance](results.json)\n",
    ]
    lines += [
        f"- [{r['image']}]({r['image']}): {r['width']}px, document {r['documentWidth']}px, main {r['mainScrollWidth']}/{r['mainWidth']}px."
        for r in records
    ]
    (args.output / "MATRIX.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"screenshots": len(records), "failures": failures}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
