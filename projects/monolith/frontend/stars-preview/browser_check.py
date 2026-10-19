"""Capture the real stars map from the branch's static fixture, never production."""

import argparse
import functools
import http.server
import json
import subprocess
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def settle_ui(page):
    # The shared reduced-motion reset still gives colors a 0.01ms transition.
    # Wait for rendered frames, including inherited-color transitions, instead
    # of assuming a wall-clock sleep means software WebGL has painted them.
    page.evaluate(
        "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    page.wait_for_function(
        "document.getAnimations().every(animation => animation.playState !== 'running')"
    )


def check(root, output, chromium_path=None, expected_sha=None):
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    if expected_sha and source_sha != expected_sha:
        raise ValueError(f"Expected {expected_sha}, checked out {source_sha}")
    built_sha = json.loads((root / "build.json").read_text())["commit"]
    if built_sha != source_sha:
        raise ValueError(
            f"Preview was built from {built_sha}, checked out {source_sha}"
        )
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
                args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
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
                            lambda route: route.fulfill(
                                content_type="text/css", body=""
                            ),
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.clock.set_fixed_time("2026-10-19T12:00:00Z")
                        record = {
                            "case": name,
                            "width": width,
                            "scheme": scheme,
                            "mode": mode,
                            "source_sha": source_sha,
                            "browser": browser.version,
                        }
                        evidence.append(record)
                        try:
                            query = (
                                "?mode=historical&month=10"
                                if mode == "historical"
                                else "?night=2026-10-19"
                            )
                            page.goto(base + query, wait_until="networkidle")
                            page.wait_for_function(
                                "window.starsFixtureMap?.loaded() && window.starsFixtureMap.getSource('sites-cells')",
                                timeout=60000,
                            )
                            page.evaluate("document.fonts.ready")
                            assert page.locator(".chrome button.scheme").count() == 1
                            assert page.locator("main.td.sheet").count() == 1
                            assert page.locator(".md-nav").count() == 0
                            settle_ui(page)
                            record["collapsed_width"] = page.evaluate(
                                "document.documentElement.scrollWidth"
                            )
                            assert record["collapsed_width"] <= width + 1, (
                                "collapsed page overflow"
                            )
                            # Observe identity before any interactions, never substitute the map.
                            page.evaluate("""() => {
                              const m = window.starsFixtureMap;
                              window.originalStarsMap = m;
                              window.originalSource = m.getSource('sites-cells');
                              window.originalCanvas = m.getCanvas();
                            }""")
                            canvas = page.locator("canvas.maplibregl-canvas")
                            canvas.focus()
                            assert (
                                canvas.evaluate(
                                    "el => getComputedStyle(el).outlineStyle"
                                )
                                != "none"
                            )
                            before_pan = page.evaluate(
                                "window.starsFixtureMap.getCenter().lng"
                            )
                            canvas.press("ArrowRight")
                            page.wait_for_function("!window.starsFixtureMap.isMoving()")
                            assert (
                                page.evaluate("window.starsFixtureMap.getCenter().lng")
                                != before_pan
                            ), "keyboard pan failed"
                            before_zoom = page.evaluate(
                                "window.starsFixtureMap.getZoom()"
                            )
                            page.get_by_role(
                                "button", name="Zoom in", exact=True
                            ).focus()
                            page.get_by_role(
                                "button", name="Zoom in", exact=True
                            ).press("Enter")
                            page.wait_for_function("!window.starsFixtureMap.isMoving()")
                            assert (
                                page.evaluate("window.starsFixtureMap.getZoom()")
                                > before_zoom
                            ), "keyboard zoom failed"
                            page.evaluate(
                                "window.starsFixtureMap.jumpTo({center: [-4.633, 56.386], zoom: 10})"
                            )
                            page.wait_for_function("window.starsFixtureMap.loaded()")
                            canvas.focus()
                            canvas.press("Enter")
                            page.locator(".card").wait_for()
                            assert (
                                page.locator(".card-loc")
                                .get_attribute("href")
                                .startswith("https://www.google.com/maps/search/")
                            )
                            if mode == "historical":
                                assert (
                                    page.get_by_role(
                                        "img",
                                        name="Clear dark hours for each month of the year",
                                    ).count()
                                    == 1
                                )
                            else:
                                assert (
                                    page.get_by_role(
                                        "columnheader", name="Cloud"
                                    ).count()
                                    == 1
                                )

                            initial = page.evaluate("""() => {
                              const m = window.starsFixtureMap;
                              return {center: m.getCenter(), zoom: m.getZoom(),
                                layers: m.getStyle().layers.map(layer => layer.id),
                                sources: Object.keys(m.getStyle().sources),
                                paint: m.getPaintProperty('sites-cells', 'fill-color'),
                                background: m.getPaintProperty('background', 'background-color')};
                            }""")
                            original_url = page.url
                            paints = []
                            toggle = page.locator("button.scheme")
                            for _ in range(4):
                                toggle.focus()
                                assert (
                                    toggle.evaluate(
                                        "el => getComputedStyle(el).outlineStyle"
                                    )
                                    != "none"
                                )
                                toggle.press("Enter")
                                settle_ui(page)
                                current = page.evaluate("""() => {
                                  const m = window.starsFixtureMap;
                                  if (m !== window.originalStarsMap || m.getSource('sites-cells') !== window.originalSource || m.getCanvas() !== window.originalCanvas) throw Error('map identity changed');
                                  return {center: m.getCenter(), zoom: m.getZoom(),
                                    layers: m.getStyle().layers.map(layer => layer.id),
                                    sources: Object.keys(m.getStyle().sources),
                                    paint: m.getPaintProperty('sites-cells', 'fill-color'),
                                    background: m.getPaintProperty('background', 'background-color')};
                                }""")
                                assert current["center"] == initial["center"]
                                assert current["zoom"] == initial["zoom"]
                                assert current["layers"] == initial["layers"]
                                assert current["sources"] == initial["sources"]
                                assert page.locator(".card").count() == 1
                                assert page.url == original_url
                                paints.append(current)
                            assert paints[0]["paint"] != initial["paint"], (
                                "heat paint did not follow scheme"
                            )
                            assert paints[0]["background"] != initial["background"], (
                                "basemap did not follow scheme"
                            )
                            assert paints[1] == initial

                            # Existing URL-backed filter remains keyboard operable, then open it
                            # again for the evidence capture with the selected-site panel visible.
                            filter_toggle = page.locator(".filter-toggle")
                            filter_toggle.focus()
                            filter_toggle.press("Enter")
                            assert (
                                filter_toggle.get_attribute("aria-expanded") == "true"
                            )
                            choice = page.get_by_role(
                                "button",
                                name="Dec" if mode == "historical" else "Tue 20",
                                exact=True,
                            )
                            choice.focus()
                            assert (
                                choice.evaluate(
                                    "el => getComputedStyle(el).outlineStyle"
                                )
                                != "none"
                            )
                            choice.press("Enter")
                            assert (
                                "month=12"
                                if mode == "historical"
                                else "night=2026-10-20"
                            ) in page.url
                            # Existing filter changes may close details; select the current row.
                            canvas.focus()
                            canvas.press("Enter")
                            page.locator(".card").wait_for()
                            filter_toggle.focus()
                            filter_toggle.press("Enter")
                            settle_ui(page)
                            record["overflow"] = page.evaluate("""() => ({
                              document: document.documentElement.scrollWidth,
                              viewport: innerWidth,
                              expanded: document.querySelector('.filter-toggle').getAttribute('aria-expanded'),
                              selected: !!document.querySelector('.card')
                            })""")
                            assert record["overflow"]["document"] <= width + 1, (
                                "horizontal page overflow"
                            )
                            assert record["overflow"]["expanded"] == "true"
                            record["contrast"] = page.evaluate("""() => {
                              const c = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
                              const rgba = color => { c.clearRect(0,0,1,1); c.fillStyle=color; c.fillRect(0,0,1,1); return [...c.getImageData(0,0,1,1).data]; };
                              const over = (fg,bg) => fg.slice(0,3).map((v,i) => v*fg[3]/255 + bg[i]*(1-fg[3]/255));
                              const background = el => {
                                const color = rgba(getComputedStyle(el).backgroundColor);
                                if (color[3] === 255) return color.slice(0,3);
                                if (!el.parentElement) throw Error('No opaque background for contrast check');
                                return over(color,background(el.parentElement));
                              };
                              const lum = values => values.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i],0);
                              return ['.stats','.legend','.card-title','.card-loc','.filter-current','.seg[aria-pressed="true"]','.month-chip:not(.is-off),.night-chip:not(.is-off)'].map(selector => {
                                const el=document.querySelector(selector), color=getComputedStyle(el).color, surface=background(el);
                                const fg=lum(over(rgba(color),surface)), bg=lum(surface);
                                return {selector, color, background:surface, ratio: (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};
                              });
                            }""")
                            assert all(
                                item["ratio"] >= 4.5 for item in record["contrast"]
                            ), record["contrast"]
                            record["paint"] = initial
                            record["scheme_paints"] = paints
                            record["keyboard"] = {
                                "pan": True,
                                "zoom": True,
                                "selection": True,
                                "filter": True,
                                "scheme": True,
                            }
                            record["screenshots"] = [name + ".png"]
                            record["errors"] = errors
                            record["map_errors"] = page.evaluate(
                                "window.starsFixtureErrors"
                            )
                            assert not errors, errors
                            assert not record["map_errors"], record["map_errors"]
                            page.evaluate("window.scrollTo(0, 0)")
                            page.screenshot(
                                path=str(output / (name + ".png")), full_page=True
                            )
                            record["passed"] = True
                        except Exception as error:
                            record["error"] = str(error)
                            record["errors"] = errors
                            page.screenshot(
                                path=str(output / (name + "-failure.png")),
                                full_page=True,
                            )
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
    parser.add_argument(
        "--expected-sha", help="Reject evidence from a different checkout"
    )
    args = parser.parse_args()
    check(
        args.artifact.resolve(), args.output.resolve(), args.chromium, args.expected_sha
    )
