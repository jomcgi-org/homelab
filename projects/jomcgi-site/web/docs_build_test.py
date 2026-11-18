"""Published docs must equal the shared, filtered manifest, with no extra pages."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = (
    Path(os.environ["TEST_SRCDIR"]) / os.environ.get("TEST_WORKSPACE", "_main")
    if "TEST_SRCDIR" in os.environ
    else Path(__file__).resolve().parents[3]
)
WEB = ROOT / "projects/jomcgi-site/web"
TOOLS = ROOT / "projects/monolith/knowledge/tools"
sys.path.insert(0, str(TOOLS))
import gen_docs_manifest as generator  # noqa: E402


def test_published_pages_are_exactly_the_filtered_manifest():
    manifest = json.loads((WEB / "docs-manifest.json").read_text())
    candidates = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*.md")]
    expected = generator.build_manifest(ROOT, candidates)
    assert manifest == expected
    for _project, directory in generator.PUBLIC_PROJECTS:
        assert f"{directory}/README.md" in {entry["path"] for entry in manifest}

    dist = WEB / "dist"
    pages = {str(path.relative_to(dist)) for path in dist.rglob("*.html")}
    assert pages == {
        "index.html",
        "404.html",
        "cv.html",
        "docs.html",
        "engineering.html",
        "factory/index.html",
        "blog.html",
        "blog/ember-conformance.html",
        "blog/125b-on-a-4090.html",
        "llm-leaderboard.html",
        "scotland.html",
        "scotland/privacy.html",
        "scotland/support.html",
        "scotland/terms.html",
        "slop.html",
        *(f"docs/{entry['slug']}.html" for entry in manifest),
    }
    for entry in manifest:
        page = (dist / f"docs/{entry['slug']}.html").read_text()
        assert f"https://jomcgi.dev/docs/{entry['slug']}" in page
        assert f'<h1 id="doc-h">{html.escape(entry["title"], quote=False)}</h1>' in page
        assert '<div id="doc-body" class="docs-prose">' in page
        assert "<!--ssr-->" not in page
    assert not list(dist.rglob("*manifest*"))
    # The Scotland catalog is an intentional public artifact from HOME-207.
    assert {str(path.relative_to(dist)) for path in dist.rglob("*.json")} == {
        "scotland/marketplace.json"
    }
    assert not (dist / "docs/_src").exists()


def test_diagrams_are_published_as_svg_without_inline_styles_or_runtime():
    dist = WEB / "dist"
    manifest = json.loads((WEB / "docs-manifest.json").read_text())
    diagrams = 0
    for entry in manifest:
        expected = len(re.findall(r"^```mermaid\s*$", entry["content"], re.MULTILINE))
        page = (dist / f"docs/{entry['slug']}.html").read_text()
        assert page.count('class="docs-diagram"') == expected, entry["slug"]
        svgs = re.findall(
            r'<svg\b[^>]*aria-describedby="docs-diagram-\d+-description"[^>]*>', page
        )
        assert len(svgs) == expected, entry["slug"]
        assert all('role="img"' in svg for svg in svgs)
        descriptions = re.findall(
            r'<desc id="docs-diagram-\d+-description">(.*?)</desc>', page, re.DOTALL
        )
        assert len(descriptions) == expected, entry["slug"]
        assert all(len(html.unescape(text)) > 40 for text in descriptions)
        assert page.count('class="docs-diagram-description"') == expected
        assert not re.search(r"<style\b|\sstyle=", page), entry["slug"]
        diagrams += expected
    assert diagrams > 0
    assert all("mermaidAPI" not in path.read_text() for path in dist.rglob("*.js"))


def test_non_public_candidates_never_enter_the_manifest(tmp_path):
    private = "projects/embervm/nested/README.md"
    outside = "projects/private/README.md"
    for path in (private, outside, "AGENTS.md", "docs/agents/internal.md"):
        source = tmp_path / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# Excluded\n\nUNPUBLISHED_SENTINEL\n")
    assert (
        generator.build_manifest(
            tmp_path, [private, outside, "AGENTS.md", "docs/agents/internal.md"]
        )
        == []
    )


@pytest.mark.parametrize(
    "marker", ["op://example/item", "10.1.2.3", "api.svc.cluster.local", "s3://example"]
)
def test_content_guard_stops_generation_before_publication(tmp_path, marker):
    paths = [f"{directory}/README.md" for _, directory in generator.PUBLIC_PROJECTS]
    for path in paths:
        source = tmp_path / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "# Public document\n\n" + (marker if path == paths[0] else "Public text")
        )
    output = tmp_path / "docs-manifest.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "BUILD_WORKSPACE_DIRECTORY"
    }
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "gen_docs_manifest.py"),
            "--out",
            str(output),
            *paths,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "public doc contains" in result.stderr
    assert not output.exists()
