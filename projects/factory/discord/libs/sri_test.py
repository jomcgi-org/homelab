"""Each page loads the house design system from /_lib/ with matching hashes."""

import base64
import hashlib
import os
import re
from pathlib import Path

LIBS = dict(
    zip(os.environ["LIB_NAMES"].split(), os.environ["LIB_FILES"].split(), strict=True)
)
PAGES = os.environ["PAGES"].split()
REF = re.compile(r'(?:src|href)="/_lib/([^"]+)"\s+integrity="([^"]+)"')
# What every page loads: the stylesheet, then the six scripts in order.
HOUSE = [
    "house-1.css",
    "lucide-1.72.0.min.js",
    "mermaid-12.3.0.min.js",
    "vega-6.4.0.min.js",
    "vega-lite-6.5.0.min.js",
    "vega-embed-7.3.0.min.js",
    "house-1.js",
]


# Published pages pin these hashes: a change to house-1 breaks them, so a
# change goes in a new file (house-2) with a new entry here.
FROZEN = {
    "house-1.css": "sha384-oWMsm4jALoVOD2EWe0AkXNEKEt+4L3gH2oWjZ4puAs/OZARfQJFLZBG+Tlezyhl1",
    "house-1.js": "sha384-9EOYVkdNo9W4GjxIL8cWRcTyJMNXZjkZn/PalibQakU5HfCPm2WvBFRQ6of/891+",
}


def sri(name):
    digest = hashlib.sha384(Path(LIBS[name]).read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode()


def test_pages_load_the_house_files_with_their_hashes():
    for page in PAGES:
        refs = REF.findall(Path(page).read_text())
        assert [name for name, _ in refs] == HOUSE, page
        for name, integrity in refs:
            assert integrity == sri(name), (page, name)


def test_pages_load_nothing_else():
    for page in PAGES:
        html = Path(page).read_text()
        for value in re.findall(r"""(?:src|href)\s*=\s*["']([^"']*)""", html):
            assert value.startswith(("/_lib/", "#")), (page, value)
        assert "@import" not in html and "url(" not in html, page


def test_the_stylesheet_loads_fonts_from_lib_only():
    css = Path(LIBS["house-1.css"]).read_text()
    for url in re.findall(r"url\(\"?([^\")]+)", css):
        assert url.startswith("/_lib/"), url
        assert url.removeprefix("/_lib/") in LIBS, url


def test_house_files_are_frozen():
    for name, integrity in FROZEN.items():
        assert sri(name) == integrity, f"{name} changed: add house-2 instead"
