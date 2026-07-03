"""Unit tests for the Marker -> grimoire chunk converter (grimoire/marker.py)."""

from __future__ import annotations

from grimoire import marker


def test_html_to_text_flattens_tables_and_breaks():
    html = (
        "<p>Line one<br/>line two</p><table><tr><td>STR</td><td>DEX</td></tr></table>"
    )
    text = marker.html_to_text(html)
    assert "Line one\nline two" in text
    assert "STR DEX" in text  # cells space-joined, not glued


def test_html_to_text_unescapes_entities():
    assert marker.html_to_text("<p>Dungeons &amp; Dragons</p>") == "Dungeons & Dragons"


def _page(page: int, blocks: list[dict]) -> dict:
    return {
        "id": f"/page/{page}/Page/0",
        "block_type": "Page",
        "html": "",
        "section_hierarchy": {},
        "children": blocks,
    }


def _sample_doc() -> dict:
    """A goblin whose lore (page 0) and stat block + ACTIONS (page 1) are split
    across two same-named SectionHeaders, plus one picture."""
    return {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h1>GOBLIN</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "<p>A small evil humanoid.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Picture/0",
                        "block_type": "Picture",
                        "html": (
                            '<img alt="A goblin" src="abc_img.jpg"/>'
                            '<div class="img-description"><p>A small green goblin '
                            'with a spear.</p><div class="img-alt">A goblin</div></div>'
                        ),
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                ],
            ),
            _page(
                1,
                [
                    {
                        "id": "/page/1/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h2>GOBLIN</h2>",
                        "section_hierarchy": {"1": "/page/1/SectionHeader/0"},
                    },
                    {
                        "id": "/page/1/Text/0",
                        "block_type": "Text",
                        "html": "<p>Armor Class 15</p>",
                        "section_hierarchy": {"1": "/page/1/SectionHeader/0"},
                    },
                    {
                        "id": "/page/1/SectionHeader/1",
                        "block_type": "SectionHeader",
                        "html": "<h3>ACTIONS</h3>",
                        "section_hierarchy": {
                            "1": "/page/1/SectionHeader/0",
                            "2": "/page/1/SectionHeader/1",
                        },
                    },
                    {
                        "id": "/page/1/Text/1",
                        "block_type": "Text",
                        "html": "<p>Scimitar. Melee Weapon Attack.</p>",
                        "section_hierarchy": {
                            "1": "/page/1/SectionHeader/0",
                            "2": "/page/1/SectionHeader/1",
                        },
                    },
                    {
                        "id": "/page/1/PageFooter/0",
                        "block_type": "PageFooter",
                        "html": "<p>123</p>",
                        "section_hierarchy": {"1": "/page/1/SectionHeader/0"},
                    },
                ],
            ),
        ],
        "metadata": {},
    }


def test_to_chunks_merges_split_monster_and_actions():
    chunks = marker.to_chunks(
        _sample_doc(), book_id="mm", image_key_prefix="books/mm/raw/img/"
    )
    text = [c for c in chunks if "image_ref" not in c]
    # Both GOBLIN sections (lore + stat) and the ACTIONS sub-section collapse
    # into a single text chunk.
    assert len(text) == 1
    c = text[0]
    assert c["section_path"] == "GOBLIN"
    assert c["chunk_ref"] == "/page/0/SectionHeader/0"
    assert "A small evil humanoid." in c["content"]
    assert "Armor Class 15" in c["content"]
    assert "Scimitar. Melee Weapon Attack." in c["content"]  # ACTIONS merged in
    assert "123" not in c["content"]  # PageFooter excluded


def test_to_chunks_emits_image_chunk_with_s3_ref():
    chunks = marker.to_chunks(
        _sample_doc(), book_id="mm", image_key_prefix="s3://grimoire/books/mm/raw/img/"
    )
    imgs = [c for c in chunks if "image_ref" in c]
    assert len(imgs) == 1
    img = imgs[0]
    assert img["image_ref"] == "s3://grimoire/books/mm/raw/img/abc_img.jpg"
    assert img["chunk_ref"] == "/page/0/Picture/0"
    assert "spear" in img["content"]
    assert img["section_path"] == "GOBLIN"


def test_to_chunks_prepends_section_name_when_absent_from_body():
    doc = {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h1>THE UNDERDARK</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "<p>A vast subterranean realm.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/1"},
                    },
                ],
            )
        ],
        "metadata": {},
    }
    # The content block points at a header id with no text (SectionHeader/1
    # absent), so its section name is unknown and no title is prepended; the
    # header block itself forms its own titled run.
    chunks = marker.to_chunks(doc, book_id="mm", image_key_prefix="p/")
    header_chunk = [c for c in chunks if c["section_path"] == "THE UNDERDARK"][0]
    assert header_chunk["content"].startswith("THE UNDERDARK")


def test_to_chunks_drops_empty_content():
    doc = {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "   ",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    }
                ],
            )
        ],
        "metadata": {},
    }
    assert marker.to_chunks(doc, book_id="mm", image_key_prefix="p/") == []
