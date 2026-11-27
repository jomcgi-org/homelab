"""Each page loads every pinned library from /_lib/ with its sha384."""

import base64
import hashlib
import os
import re
from pathlib import Path

# (path of the shipped file, its name under /_lib/)
LIBS = list(
    zip(os.environ["LIB_FILES"].split(), os.environ["LIB_NAMES"].split(), strict=True)
)
PAGES = os.environ["PAGES"].split()
SCRIPT = re.compile(r'<script\s+src="/_lib/([^"]+)"\s+integrity="([^"]+)"')


def sri(path):
    digest = hashlib.sha384(Path(path).read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode()


def test_pages_load_the_pinned_libraries():
    want = {name: sri(path) for path, name in LIBS}
    assert len(want) == 4
    for page in PAGES:
        got = dict(SCRIPT.findall(Path(page).read_text()))
        assert got == want, page


def test_pages_load_nothing_else():
    for page in PAGES:
        html = Path(page).read_text()
        assert len(re.findall(r"<script\s+src=", html)) == 4, page
        for external in ("http:", "https:", "<link", "<img", "@import", "url("):
            assert external not in html, (page, external)
