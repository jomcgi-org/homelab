"""Real-CSS production-route checks, including server HTML with scripts disabled."""

import argparse
import functools
import hashlib
import http.server
import json
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if not url.path.startswith("/assets/") and url.path != "/build.json":
            self.path = "/banner.html" if "banner=1" in url.query else "/index.html"
        super().do_GET()

    def log_message(self, *_args):
        pass


def check(root, output, expected_sha):
    assert json.loads((root / "build.json").read_text())["commit"] == expected_sha
    exact = bool(re.fullmatch("[a-f0-9]{40}", expected_sha))
    if exact:
        assert (
            subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            == expected_sha
        )
        assert not subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ).strip(), "exact-head capture requires a clean worktree"
    else:
        assert expected_sha == "local-unpublished"
    output.mkdir(parents=True, exist_ok=True)
    assert not list(output.glob("*.png")) and not (output / "checks.json").exists(), (
        "use fresh capture output"
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    receipt = {
        "commit": expected_sha,
        "exact_head": exact,
        "status": "failed",
        "checks": [],
        "limitations": [
            "Production root/public/error CSS and production components, synthetic stats and history adapter. No backend or real-host routing.",
            "Agents HTML is server rendered and hydrated. Other direct fixture routes mount client-side. Kit server loads and error-preload timing are not reproduced.",
            "Local Schibsted font; Google font CSS is blocked. System mono fallback can vary by runner.",
            "Engineering has no direct /ember/agents link at the task base; its fixture navigation link tests the transition without adding production copy.",
        ],
    }
    paints = set()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            receipt["chromium"] = browser.version
            for width in (390, 1440):
                for scheme in ("light", "dark"):
                    normal_text = None
                    for mode in ("normal", "reduced", "no-js"):
                        context = browser.new_context(
                            viewport={"width": width, "height": 1000},
                            color_scheme=scheme,
                            reduced_motion="reduce"
                            if mode == "reduced"
                            else "no-preference",
                            java_script_enabled=mode != "no-js",
                            service_workers="block",
                        )
                        errors = []
                        context.route(
                            re.compile(r"https://fonts\.(googleapis|gstatic)\.com/"),
                            lambda route: route.fulfill(
                                content_type="text/css", body=""
                            ),
                        )
                        page = context.new_page()
                        page.on(
                            "pageerror",
                            lambda error, errors=errors: errors.append(str(error)),
                        )
                        page.on(
                            "console",
                            lambda message, errors=errors: (
                                errors.append(message.text)
                                if "hydration_mismatch" in message.text
                                else None
                            ),
                        )
                        page.goto(base + "/ember/agents", wait_until="networkidle")
                        page.locator(".agent-sheet").wait_for()
                        page.evaluate("document.fonts.ready")
                        assert page.evaluate(
                            "document.fonts.check('16px \"Schibsted Grotesk\"')"
                        ), "fixture font failed to load"
                        assert (
                            page.title()
                            == "One microVM per agent session · ember/agents"
                        )
                        assert "Credentials stay outside the VM." in page.locator(
                            'meta[name="description"]'
                        ).get_attribute("content")
                        assert page.locator(".chrome").count() == 1
                        assert page.locator("button.scheme").count() == 1
                        assert page.locator("main, h1").count() == 2
                        assert (
                            page.locator(".md-nav, .ember-site, .scroller").count() == 0
                        )
                        assert page.locator("[data-phase]").count() == 5
                        assert page.locator("[data-at]").count() == 11
                        assert page.locator(".agent-calls code").count() == 14
                        assert (
                            page.locator(
                                'nav[aria-label="Keep going"] a[href="/ember/firecracker"]'
                            ).count()
                            == 1
                        )
                        assert page.locator('a[href="/ember"]').count() >= 1
                        for item in page.locator(
                            "[data-at], .agent-calls code, .dbox"
                        ).all():
                            assert item.is_visible(), item.inner_text()
                        text = page.locator(".agent-sheet").inner_text()
                        if normal_text is None:
                            normal_text = text
                        assert text == normal_text, "static paths lost content"

                        def geometry(
                            label, page=page, mode=mode, scheme=scheme, width=width
                        ):
                            measured = page.evaluate("""() => ({
                                viewport:innerWidth, viewportHeight:innerHeight, document:document.documentElement.scrollWidth,
                                body:document.body.scrollWidth, bodyHeight:document.body.scrollHeight,
                                documentHeight:document.documentElement.scrollHeight, scrollY,
                                bodyOverflow:getComputedStyle(document.body).overflow,
                                inlineOverflow:document.body.style.overflow,
                                diagramOverflow:[...document.querySelectorAll('.agent-sheet .fig-art')].map(el=>el.scrollWidth-el.clientWidth),
                                diagramFont:document.querySelector('.agent-sheet .dbox') ? getComputedStyle(document.querySelector('.agent-sheet .dbox')).fontSize : null,
                                background:document.querySelector('main') ? getComputedStyle(document.querySelector('main')).backgroundColor : null,
                                ink:document.querySelector('main') ? getComputedStyle(document.querySelector('main')).color : null
                            })""")
                            assert measured["document"] <= measured["viewport"] + 1, (
                                measured
                            )
                            assert measured["body"] <= measured["viewport"] + 1, (
                                measured
                            )
                            assert all(
                                value <= 1 for value in measured["diagramOverflow"]
                            ), measured
                            assert measured["inlineOverflow"] == "", measured
                            if measured["diagramFont"]:
                                assert (
                                    float(measured["diagramFont"].removesuffix("px"))
                                    >= 12
                                )
                            receipt["checks"].append(
                                {
                                    "mode": mode,
                                    "scheme": scheme,
                                    "width": width,
                                    "state": label,
                                    **measured,
                                }
                            )

                        geometry("opening")
                        opening_paint = receipt["checks"][-1]["background"]
                        paints.add(opening_paint)
                        name = f"agents-{width}-{scheme}-{mode}"
                        page.screenshot(path=str(output / f"{name}-opening.png"))
                        page.screenshot(
                            path=str(output / f"{name}-full.png"), full_page=True
                        )
                        page.locator("#resume").scroll_into_view_if_needed()
                        geometry("resume")
                        assert page.evaluate("scrollY") > 0
                        page.screenshot(path=str(output / f"{name}-resume.png"))
                        assert (
                            page.locator(".agent-sheet").evaluate(
                                "el => el.getAnimations({subtree:true}).length"
                            )
                            == 0
                        )
                        if mode != "no-js":
                            page.set_viewport_size(
                                {"width": 1440 if width == 390 else 390, "height": 844}
                            )
                            geometry("resize")
                            page.set_viewport_size({"width": width, "height": 1000})
                            page.emulate_media(reduced_motion="reduce")
                            assert (
                                page.locator(".agent-sheet").inner_text() == normal_text
                            )
                            toggle = page.locator("button.scheme")
                            toggle.focus()
                            assert (
                                toggle.evaluate(
                                    "el => getComputedStyle(el).outlineStyle"
                                )
                                != "none"
                            )
                            page.keyboard.press("Enter")
                            opposite = "dark" if scheme == "light" else "light"
                            assert (
                                page.evaluate("document.documentElement.dataset.theme")
                                == opposite
                            )
                            assert (
                                page.evaluate("localStorage.getItem('td-theme')")
                                == opposite
                            )
                            # global.css gives reduced-motion transitions a
                            # 0.01ms duration. Observe the painted scheme after
                            # that frame instead of racing its starting value.
                            page.wait_for_function(
                                "paint => getComputedStyle(document.querySelector('.agent-sheet')).backgroundColor !== paint",
                                arg=opening_paint,
                            )
                            page.reload(wait_until="networkidle")
                            assert (
                                page.evaluate("document.documentElement.dataset.theme")
                                == opposite
                            )
                            page.locator("button.scheme").focus()
                            page.keyboard.press("Space")
                            assert (
                                page.evaluate("document.documentElement.dataset.theme")
                                == scheme
                            )
                            page.wait_for_function(
                                "paint => getComputedStyle(document.querySelector('.agent-sheet')).backgroundColor === paint",
                                arg=opening_paint,
                            )
                            page.goto(base + "/engineering", wait_until="networkidle")
                            page.locator("main.eng-page").wait_for()
                            page.evaluate("window.fixtureNavigationSentinel = true")
                            page.get_by_role(
                                "link", name="Agents fixture", exact=True
                            ).click()
                            page.locator(".agent-sheet").wait_for()
                            geometry("engineering-to-agents")
                            assert (
                                page.evaluate("window.fixtureNavigationSentinel")
                                is True
                            )
                            page.locator(
                                'nav[aria-label="Keep going"] a[href="/ember"]'
                            ).click()
                            page.locator(".ember-site").wait_for()
                            assert page.locator(".chrome, button.scheme").count() == 0
                            geometry("agents-to-legacy")
                            legacy_paint = page.locator(".ember-site").evaluate(
                                "el=>({background:getComputedStyle(el).backgroundColor,font:getComputedStyle(el).fontFamily})"
                            )
                            page.locator(".ember-site details").filter(
                                has=page.locator('a[href="/ember/agents"]')
                            ).locator("summary").click()
                            page.locator(
                                '.ember-site a[href="/ember/agents"]'
                            ).first.click()
                            page.locator(".agent-sheet").wait_for()
                            assert page.locator(".chrome, button.scheme").count() == 2
                            assert (
                                page.evaluate("document.documentElement.dataset.theme")
                                == scheme
                            )
                            geometry("legacy-to-agents")
                            page.get_by_role(
                                "link", name="Engineering fixture", exact=True
                            ).click()
                            page.locator("main.eng-page").wait_for()
                            geometry("back-to-engineering")
                            page.go_back(wait_until="networkidle")
                            page.locator(".agent-sheet").wait_for()
                            assert (
                                page.evaluate("window.fixtureNavigationSentinel")
                                is True
                            )
                            page.goto(base + "/ember", wait_until="networkidle")
                            page.locator(".ember-site").wait_for()
                            assert (
                                page.locator(".ember-site").evaluate(
                                    "el=>({background:getComputedStyle(el).backgroundColor,font:getComputedStyle(el).fontFamily})"
                                )
                                == legacy_paint
                            )

                        page.goto(
                            base + "/ember/agents?banner=1", wait_until="networkidle"
                        )
                        assert (
                            page.locator(
                                '.maintenance-banner[role="status"]'
                            ).inner_text()
                            == "Synthetic maintenance notice"
                        )
                        geometry("maintenance-banner")
                        page.screenshot(path=str(output / f"{name}-banner.png"))
                        page.locator("#keep-going").scroll_into_view_if_needed()
                        geometry("maintenance-banner-later")
                        assert page.evaluate("scrollY") > 0
                        assert not errors, errors
                        context.close()
            browser.close()
        assert len(paints) == 2, "light and dark must have distinct sheet paint"
        receipt["screenshots"] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.glob("*.png"))
        }
        assert len(receipt["screenshots"]) == 48
        receipt["status"] = "passed"
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        server.shutdown()
        server.server_close()
        (output / "checks.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    check(args.root.resolve(), args.output.resolve(), args.expected_sha)
