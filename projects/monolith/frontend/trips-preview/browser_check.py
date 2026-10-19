"""Capture all trips sheets with real layouts and a local synthetic basemap."""

import argparse
import base64
import functools
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROUTES = {
    "index": "/app/trips",
    "trip": "/app/trips/fixture-walk",
    "day": "/app/trips/fixture-walk/day/1",
    "timeline": "/app/trips/fixture-walk/timeline",
}
# Offline style with no glyphs, sprites or remote sources. Production route,
# marker and control code still runs in MapLibre's real WebGL renderer.
STYLE = {
    "version": 8,
    "sources": {},
    "layers": [
        {"id": "ground", "type": "background", "paint": {"background-color": "white"}},
    ],
}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
LIMITATIONS = (
    "Invented route, synthetic image swatches, blank local basemap and DEM. "
    "Real WebGL controls; no actual-host or geographic tile acceptance. "
    "Production water, park, wood, landuse and building fills share --paper "
    "over --sheet, so water/land contrast is not established by this fixture. "
    "Liberty natural_earth raster and building-3d fill-extrusion are not "
    "repainted or represented here. Inspect those on real tiles in both schemes."
)


def font_widths(page):
    # A minimal guest can start Chromium without any usable system fonts.
    # In that case text has zero width and even overflow checks falsely pass.
    return page.evaluate(
        """() => {
          const context = document.createElement('canvas').getContext('2d');
          return Object.fromEntries(['sans-serif', 'monospace'].map(family => {
            context.font = `16px ${family}`;
            return [family, context.measureText('Trips 0123456789').width];
          }));
        }"""
    )


def capture(page, output, record, state="page", full_page=True):
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    filename = record["name"] + ("" if state == "page" else f"-{state}") + ".png"
    receipt = {"state": state, "file": filename, "overflow_px": overflow}
    record.setdefault("screenshots", []).append(receipt)
    page.screenshot(path=str(output / filename), full_page=full_page)
    assert overflow <= 0, (filename, overflow)
    return overflow


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path.startswith("/app/trips"):
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_args):
        pass


def check(root, output, head):
    output.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--enable-unsafe-swiftshader"])
            for surface, path in ROUTES.items():
                for width in (390, 1440):
                    for scheme in ("light", "dark"):
                        context = browser.new_context(
                            viewport={"width": width, "height": 1000},
                            color_scheme=scheme,
                            reduced_motion="reduce",
                            service_workers="block",
                        )
                        context.add_init_script(
                            f"localStorage.setItem('td-theme', '{scheme}');"
                        )
                        forbidden = []
                        errors = []

                        def route_request(route):
                            url = route.request.url
                            if url.startswith(base) and not url.endswith("favicon.svg"):
                                route.continue_()
                            elif url.endswith("favicon.svg") or url.startswith(
                                "https://fonts.googleapis.com/"
                            ):
                                route.fulfill(content_type="text/css", body="")
                            elif url in (
                                "https://tiles.openfreemap.org/styles/liberty",
                                "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
                            ):
                                route.fulfill(
                                    content_type="application/json",
                                    body=json.dumps(STYLE),
                                )
                            elif url.startswith(
                                "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/"
                            ):
                                route.fulfill(content_type="image/png", body=PNG)
                            else:
                                forbidden.append(url)
                                route.abort()

                        context.route("**/*", route_request)
                        page = context.new_page()
                        page.on("pageerror", lambda e: errors.append(str(e)))
                        name = f"{surface}-{width}-{scheme}"
                        record = {
                            "name": name,
                            "path": path,
                            "width": width,
                            "scheme": scheme,
                            "head": head,
                        }
                        try:
                            page.goto(base + path, wait_until="networkidle")
                            page.evaluate("document.fonts.ready")
                            record["font_widths_px"] = font_widths(page)
                            assert all(
                                value > 0 for value in record["font_widths_px"].values()
                            ), (
                                "Missing browser fonts: install system sans-serif and monospace fonts"
                            )
                            assert page.locator("main.td.sheet").count() == 1
                            assert page.locator("button.scheme").count() == 1
                            assert page.locator(".md-nav").count() == 0
                            record["overflow_px"] = capture(page, output, record)
                            if surface in ("trip", "day"):
                                assert (
                                    page.locator("canvas.maplibregl-canvas").count()
                                    == 1
                                )
                            if surface == "timeline":
                                page.get_by_role(
                                    "button", name="Open photo 1", exact=True
                                ).focus()
                                page.keyboard.press("Enter")
                                page.get_by_role("dialog").wait_for()
                                assert page.get_by_role(
                                    "button", name="Close", exact=True
                                ).evaluate("e => e === document.activeElement")
                                page.keyboard.press("ArrowRight")
                                assert (
                                    page.get_by_role("dialog")
                                    .get_by_role("img")
                                    .get_attribute("alt")
                                    == "Photo 2 of 6"
                                )
                                capture(page, output, record, "viewer", full_page=False)
                                page.keyboard.press("Escape")
                                assert page.get_by_role(
                                    "button", name="Open photo 1", exact=True
                                ).evaluate("e => e === document.activeElement")
                                page.get_by_role(
                                    "button", name="trail", exact=True
                                ).click()
                                assert page.get_by_text("3 / 6 photos").count() == 1
                                page.get_by_role(
                                    "button", name="Map", exact=True
                                ).click()
                                page.wait_for_selector("canvas.maplibregl-canvas")
                                capture(page, output, record, "map")
                            if surface == "day":
                                page.get_by_role(
                                    "button", name="Zoom in", exact=True
                                ).click()
                                capture(page, output, record, "map-controls")
                                page.get_by_role(
                                    "button", name="Next photo", exact=True
                                ).click()
                                page.get_by_role(
                                    "button", name="View photo 2 fullscreen", exact=True
                                ).click()
                                fullscreen = page.get_by_role(
                                    "button", name="Close fullscreen photo", exact=True
                                )
                                fullscreen.wait_for(state="visible")
                                capture(
                                    page, output, record, "fullscreen", full_page=False
                                )
                                page.keyboard.press("Escape")
                                fullscreen.wait_for(state="hidden")
                            page.locator("button.scheme").click()
                            assert (
                                page.evaluate("document.documentElement.dataset.theme")
                                != scheme
                            )
                            if surface in ("trip", "day"):
                                assert (
                                    page.locator("canvas.maplibregl-canvas").count()
                                    == 1
                                )
                            assert not forbidden, forbidden
                            assert not errors, errors
                            record["passed"] = True
                        except Exception as error:
                            record["error"] = str(error)
                            page.screenshot(
                                path=str(output / f"{name}-failure.png"), full_page=True
                            )
                            raise
                        finally:
                            results.append(record)
                            (output / "checks.json").write_text(
                                json.dumps(
                                    {
                                        "head": head,
                                        "browser": browser.version,
                                        "complete": len(results) == 16
                                        and all(row.get("passed") for row in results),
                                        "limitations": LIMITATIONS,
                                        "checks": results,
                                    },
                                    indent=2,
                                )
                            )
                            context.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"Passed {len(results)} trips page/width/scheme combinations at {head}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.head)
