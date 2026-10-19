"""Capture production Grimoire components using only invented local fixtures."""

import argparse
import functools
import http.server
import json
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if not Path(self.translate_path(urlsplit(self.path).path)).is_file():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *args):
        pass


ROUTES = {
    "home": ("/app/grimoire", "Grimoire"),
    "library": ("/app/grimoire/library", "The Lantern Atlas"),
    "world": ("/app/grimoire/world?e=fixture-person", "Mira of the Lantern"),
    "entity": ("/app/grimoire/entity/fixture-person", "Mira of the Lantern"),
    "adventure": ("/app/grimoire/adventure/fixture-adventure", "The Paper Tide"),
    "book": ("/app/grimoire/book/fixture-book", "Mira keeps a lantern"),
    "chat": ("/app/grimoire/chat", "Who keeps the lantern?"),
    "share": ("/app/grimoire/chat/s/fixture-share", "Who keeps the lantern?"),
}
GATED = {"world", "entity", "adventure", "chat"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != args.expected_sha:
        raise SystemExit(f"Expected {args.expected_sha}, checkout is {head}")
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", "projects/monolith/frontend"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(Handler, directory=str(args.artifact.resolve())),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    checks = []
    failures = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def visit(name, width, scheme, state="normal", motion="reduce", extra=None):
            path, text = ROUTES[name]
            if state != "normal":
                path += ("&" if "?" in path else "?") + f"state={state}"
            context = browser.new_context(
                viewport={"width": width, "height": 1000},
                color_scheme=scheme,
                reduced_motion=motion,
            )
            # Assets come from this fixture build. External fonts and analytics
            # are denied, with no backend or challenge-provider traffic.
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(origin + "/")
                    else route.fulfill(status=200, body="", content_type="text/css")
                ),
            )
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            filename = (
                f"{name}-{width}-{scheme}-{state}-{motion}"
                + (f"-{extra}" if extra else "")
                + ".png"
            )
            record = {
                "source_head": head,
                "route": path,
                "width": width,
                "scheme": scheme,
                "state": state,
                "motion": motion,
                "screenshot": filename,
                "assertions": {},
                "overflow_px": None,
            }
            try:
                if extra == "from-engineering":
                    page.goto(origin + "/engineering", wait_until="networkidle")
                    page.locator(".site-index summary").click()
                    page.locator('.site-index a[href="/app/grimoire"]').click()
                    record["assertions"]["navigation_from_public_sheet"] = True
                else:
                    page.goto(origin + path, wait_until="networkidle")
                if name in GATED:
                    assert page.evaluate("window.fixtureRequests.length") == 0, (
                        "corpus request before admission"
                    )
                    record["assertions"]["no_pre_admission_fetch"] = True
                    page.get_by_role("button", name="Solve fixture challenge").click()
                if state == "normal":
                    if name == "home":
                        page.locator(".scrollstory h1:visible").wait_for()
                    else:
                        page.get_by_text(text, exact=False).first.wait_for()
                elif state == "locked":
                    page.get_by_text("Not available to read here").wait_for()
                page.wait_for_timeout(350)
                assert page.locator(".chrome .scheme").count() == 1, (
                    "one scheme control required"
                )
                assert page.locator("main.td.sheet").count() == 1, "one sheet required"
                assert page.locator("h1, h2").count() > 0, "heading missing"
                record["assertions"]["sheet_chrome_heading"] = True
                if name == "book" and state == "normal":
                    passage_width = page.locator(".pub-panel").evaluate(
                        "e=>e.getBoundingClientRect().width"
                    )
                    record["passage_width_px"] = passage_width
                    assert passage_width >= 230, f"reader compressed: {passage_width}px"
                    record["assertions"]["readable_passage_width"] = True
                if name == "library" and state == "normal":
                    contrasts = page.locator(".cover-generated, .locked-badge").evaluate_all(
                        """elements => elements.map(e => {
                          const ctx=document.createElement('canvas').getContext('2d');
                          const luminance=color=>{
                            ctx.fillStyle=color; ctx.fillRect(0,0,1,1);
                            const rgb=[...ctx.getImageData(0,0,1,1).data].slice(0,3).map(x=>{
                              x/=255; return x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4;
                            });
                            return rgb[0]*0.2126+rgb[1]*0.7152+rgb[2]*0.0722;
                          };
                          const s=getComputedStyle(e), fg=luminance(s.color), bg=luminance(s.backgroundColor);
                          return (Math.max(fg,bg)+0.05)/(Math.min(fg,bg)+0.05);
                        })"""
                    )
                    record["cover_contrast_ratios"] = contrasts
                    assert contrasts and min(contrasts) >= 4.5, contrasts
                    record["assertions"]["cover_labels_contrast"] = True
                if extra == "source":
                    page.locator("button.touched-chip").first.click()
                    page.get_by_role("dialog").wait_for()
                elif extra == "chapters":
                    page.get_by_role(
                        "button", name="Chapters", exact=False
                    ).first.click()
                    page.get_by_text("Harbour", exact=False).first.wait_for()
                elif extra == "fork":
                    page.get_by_role("button", name="CONTINUE THIS CHAT").click()
                    assert page.locator("textarea").count() == 0
                    assert page.evaluate("window.fixtureRequests.length") == 0
                    record["assertions"]["read_only_before_fork"] = True
                elif extra and extra.startswith("scene-"):
                    progress = float(extra.split("-")[1])
                    page.evaluate(
                        "p => { const s=document.querySelector('.scrollstory'); scrollTo(0,s.offsetTop+(s.offsetHeight-innerHeight)*p); }",
                        progress,
                    )
                    page.wait_for_timeout(600)
                if name == "share":
                    assert page.locator("textarea").count() == 0
                    record["assertions"]["share_read_only"] = True
                overflow = page.evaluate(
                    "Math.max(0,document.documentElement.scrollWidth-innerWidth)"
                )
                record["overflow_px"] = overflow
                assert overflow == 0, f"horizontal document overflow: {overflow}px"
                record["assertions"]["no_horizontal_overflow"] = True
                assert not errors, errors
                record["assertions"]["no_page_errors"] = True
                page.screenshot(
                    path=str(args.output / filename), full_page=motion == "reduce"
                )
                if extra is None and state == "normal":
                    before = page.locator(".grimoire-sheet").evaluate(
                        "e=>getComputedStyle(e).backgroundColor"
                    )
                    canvas_before = (
                        page.locator("canvas").first.evaluate("e=>e.toDataURL()")
                        if page.locator("canvas").count() and name == "world"
                        else None
                    )
                    toggle = page.locator(".scheme")
                    toggle.focus()
                    assert (
                        toggle.evaluate("e=>getComputedStyle(e).outlineStyle") != "none"
                    ), "focus is not visible"
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(150)
                    assert (
                        page.locator(".grimoire-sheet").evaluate(
                            "e=>getComputedStyle(e).backgroundColor"
                        )
                        != before
                    )
                    if canvas_before:
                        assert (
                            page.locator("canvas").first.evaluate("e=>e.toDataURL()")
                            != canvas_before
                        ), "graph did not repaint"
                        record["assertions"]["canvas_scheme_repaint"] = True
                    stored = page.evaluate("localStorage.getItem('td-theme')")
                    page.reload(wait_until="networkidle")
                    assert page.locator("html").get_attribute("data-theme") == stored
                    record["assertions"]["keyboard_focus_scheme_persistence"] = True
            except Exception as error:
                record["failure"] = str(error)
                failures.append(f"{filename}: {error}")
                page.screenshot(path=str(args.output / filename), full_page=False)
            finally:
                checks.append(record)
                (args.output / "checks.json").write_text(
                    json.dumps(
                        {
                            "source_head": head,
                            "fixture_only": True,
                            "checks": checks,
                            "failures": failures,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                context.close()

        for name in ROUTES:
            for width in (390, 1440):
                for scheme in ("light", "dark"):
                    visit(name, width, scheme)
        for scheme in ("light", "dark"):
            for width in (390, 1440):
                for progress in (0.1, 0.4, 0.65, 0.9):
                    visit(
                        "home",
                        width,
                        scheme,
                        motion="no-preference",
                        extra=f"scene-{progress}",
                    )
            visit("book", 390, scheme, extra="chapters")
            visit("book", 390, scheme, state="locked")
            visit("chat", 390, scheme, extra="source")
            visit("share", 390, scheme, extra="fork")
            visit("share", 390, scheme, state="empty")
            visit("library", 390, scheme, state="empty")
            visit("library", 390, scheme, state="error")
            visit("book", 901, scheme)
            visit("home", 1440, scheme, extra="from-engineering")
        browser.close()
    server.shutdown()
    print(f"Grimoire captures: {len(checks)}, failures: {len(failures)}, head: {head}")
    for failure in failures:
        print(failure)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
