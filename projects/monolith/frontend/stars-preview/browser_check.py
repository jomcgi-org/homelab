"""Capture the real stars map from the branch's static fixture, never production."""

import argparse
import functools
import http.server
import json
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def check(root, output, chromium_path=None):
    output.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    evidence = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chromium_path,
                args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            )
            for width in [390, 1440]:
                for scheme in ["light", "dark"]:
                    for mode in ["live", "historical"]:
                        name = f"{mode}-{scheme}-{width}"
                        context = browser.new_context(
                            viewport={"width": width, "height": 1000},
                            color_scheme=scheme,
                            reduced_motion="reduce",
                            service_workers="block",
                        )
                        context.route(
                            "https://fonts.googleapis.com/**",
                            lambda route: route.fulfill(content_type="text/css", body=""),
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.clock.set_fixed_time("2026-10-19T12:00:00Z")
                        record = {"case": name, "width": width, "scheme": scheme, "mode": mode}
                        evidence.append(record)
                        try:
                            query = "?mode=historical&month=10" if mode == "historical" else "?night=2026-10-19"
                            page.goto(base + query, wait_until="networkidle")
                            page.wait_for_function(
                                "window.starsFixtureMap?.loaded() && window.starsFixtureMap.getSource('sites-cells')",
                                timeout=60000,
                            )
                            page.evaluate("document.fonts.ready")
                            assert page.locator(".chrome button.scheme").count() == 1
                            assert page.locator("main.td.sheet").count() == 1
                            assert page.locator(".md-nav").count() == 0
                            # Observe identity before any interactions, never substitute the map.
                            page.evaluate("""() => {
                              const m = window.starsFixtureMap;
                              window.originalStarsMap = m;
                              window.originalSource = m.getSource('sites-cells');
                              window.originalCanvas = m.getCanvas();
                            }""")
                            canvas = page.locator("canvas.maplibregl-canvas")
                            canvas.focus()
                            canvas.press("ArrowRight")
                            page.wait_for_timeout(350)
                            page.evaluate("window.starsFixtureMap.jumpTo({center: [-4.633, 56.386], zoom: 10})")
                            page.wait_for_function("window.starsFixtureMap.loaded()")
                            canvas.press("Enter")
                            page.locator(".card").wait_for()
                            assert page.locator(".card-loc").get_attribute("href").startswith("https://www.google.com/maps/search/")
                            if mode == "historical":
                                assert page.get_by_role("img", name="Clear dark hours for each month of the year").count() == 1
                            else:
                                assert page.get_by_role("columnheader", name="Cloud").count() == 1

                            initial = page.evaluate("""() => {
                              const m = window.starsFixtureMap;
                              return {center: m.getCenter(), zoom: m.getZoom(),
                                paint: m.getPaintProperty('sites-cells', 'fill-color'),
                                background: m.getPaintProperty('background', 'background-color')};
                            }""")
                            original_url = page.url
                            paints = []
                            toggle = page.locator("button.scheme")
                            for _ in range(4):
                                toggle.focus()
                                assert toggle.evaluate("el => getComputedStyle(el).outlineStyle") != "none"
                                toggle.press("Enter")
                                page.wait_for_timeout(100)
                                current = page.evaluate("""() => {
                                  const m = window.starsFixtureMap;
                                  if (m !== window.originalStarsMap || m.getSource('sites-cells') !== window.originalSource || m.getCanvas() !== window.originalCanvas) throw Error('map identity changed');
                                  return {center: m.getCenter(), zoom: m.getZoom(),
                                    paint: m.getPaintProperty('sites-cells', 'fill-color'),
                                    background: m.getPaintProperty('background', 'background-color')};
                                }""")
                                assert current["center"] == initial["center"]
                                assert current["zoom"] == initial["zoom"]
                                assert page.locator(".card").count() == 1
                                assert page.url == original_url
                                paints.append(current)
                            assert paints[0]["paint"] != initial["paint"], "heat paint did not follow scheme"
                            assert paints[0]["background"] != initial["background"], "basemap did not follow scheme"
                            assert paints[1] == initial

                            # Existing URL-backed filter remains keyboard operable, then open it
                            # again for the evidence capture with the selected-site panel visible.
                            filter_toggle = page.locator(".filter-toggle")
                            filter_toggle.focus()
                            filter_toggle.press("Enter")
                            assert filter_toggle.get_attribute("aria-expanded") == "true"
                            choice = page.get_by_role("button", name="Dec" if mode == "historical" else "Tue 20", exact=True)
                            choice.focus()
                            assert choice.evaluate("el => getComputedStyle(el).outlineStyle") != "none"
                            choice.press("Enter")
                            assert ("month=12" if mode == "historical" else "night=2026-10-20") in page.url
                            # Existing filter changes may close details; select the current row.
                            canvas.focus()
                            canvas.press("Enter")
                            page.locator(".card").wait_for()
                            filter_toggle.focus()
                            filter_toggle.press("Enter")
                            page.wait_for_timeout(150)
                            record["overflow"] = page.evaluate("""() => ({
                              document: document.documentElement.scrollWidth,
                              viewport: innerWidth,
                              expanded: document.querySelector('.filter-toggle').getAttribute('aria-expanded'),
                              selected: !!document.querySelector('.card')
                            })""")
                            assert record["overflow"]["document"] <= width + 1, "horizontal page overflow"
                            assert record["overflow"]["expanded"] == "true"
                            record["contrast"] = page.evaluate("""() => {
                              const c = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
                              const rgb = color => { c.clearRect(0,0,1,1); c.fillStyle=color; c.fillRect(0,0,1,1); return [...c.getImageData(0,0,1,1).data].slice(0,3); };
                              const lum = values => values.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i],0);
                              const bg = lum(rgb(getComputedStyle(document.querySelector('main')).backgroundColor));
                              return ['.stats','.legend','.card-title','.card-loc','.filter-current','.seg[aria-pressed="true"]'].map(selector => {
                                const el=document.querySelector(selector), fg=lum(rgb(getComputedStyle(el).color));
                                return {selector, ratio: (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};
                              });
                            }""")
                            assert all(item["ratio"] >= 4.5 for item in record["contrast"]), record["contrast"]
                            record["paint"] = initial
                            record["screenshots"] = [name + ".png"]
                            record["errors"] = errors
                            assert not errors, errors
                            page.evaluate("window.scrollTo(0, 0)")
                            page.screenshot(path=str(output / (name + ".png")), full_page=True)
                            record["passed"] = True
                        except Exception as error:
                            record["error"] = str(error)
                            record["errors"] = errors
                            page.screenshot(path=str(output / (name + "-failure.png")), full_page=True)
                            raise
                        finally:
                            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (output / "checks.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chromium", help="Optional local Chromium executable")
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.chromium)
