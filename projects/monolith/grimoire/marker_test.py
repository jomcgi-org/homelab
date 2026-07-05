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
    chunks = marker.to_chunks(_sample_doc(), image_key_prefix="books/mm/raw/img/")
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
        _sample_doc(), image_key_prefix="s3://grimoire/books/mm/raw/img/"
    )
    imgs = [c for c in chunks if "image_ref" in c]
    assert len(imgs) == 1
    img = imgs[0]
    assert img["image_ref"] == "s3://grimoire/books/mm/raw/img/abc_img.jpg"
    assert img["chunk_ref"] == "/page/0/Picture/0"
    assert "spear" in img["content"]
    assert img["section_path"] == "GOBLIN"


def test_to_chunks_interleaves_images_in_document_order():
    doc = {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h1>ABOLETH</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Text/1",
                        "block_type": "Text",
                        "html": "<p>Ancient aberration lore.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Picture/2",
                        "block_type": "Picture",
                        "html": '<img src="ab_img.jpg" alt="an aboleth lurking">',
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/SectionHeader/3",
                        "block_type": "SectionHeader",
                        "html": "<h1>BEHOLDER</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/3"},
                    },
                    {
                        "id": "/page/0/Text/4",
                        "block_type": "Text",
                        "html": "<p>Floating tyrant lore.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/3"},
                    },
                ],
            )
        ],
        "metadata": {},
    }
    chunks = marker.to_chunks(doc, image_key_prefix="pfx/")
    kinds = ["image" if "image_ref" in c else "text" for c in chunks]
    # The plate sits between the two sections, exactly where it appears in the
    # book (the loader assigns seq from line order, and the reader trusts seq
    # to reconstruct print order), not appended after all text chunks.
    assert kinds == ["text", "image", "text"]
    assert chunks[0]["section_path"] == "ABOLETH"
    assert chunks[1]["image_ref"] == "pfx/ab_img.jpg"
    assert chunks[2]["section_path"] == "BEHOLDER"


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
    chunks = marker.to_chunks(doc, image_key_prefix="p/")
    header_chunk = [c for c in chunks if c["section_path"] == "THE UNDERDARK"][0]
    assert header_chunk["content"].startswith("THE UNDERDARK")


def test_to_chunks_new_header_with_stale_hierarchy_does_not_bleed():
    """A heading that opens a new section but whose section_hierarchy still
    points at the *previous* header (real Marker output: the new header's own
    entry is not yet on the running stack) must start its own chunk, not append
    its title to the prior section's body."""
    doc = {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h1>HOW TO USE THIS BOOK</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "<p>Pick up the Starter Set.</p>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        # The next section's heading, but Marker still reports the
                        # PREVIOUS header at its deepest level, not itself.
                        "id": "/page/0/SectionHeader/1",
                        "block_type": "SectionHeader",
                        "html": "<h1>WHAT IS A MONSTER?</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                ],
            )
        ],
        "metadata": {},
    }
    chunks = marker.to_chunks(doc, image_key_prefix="p/")
    text = [c for c in chunks if "image_ref" not in c]
    assert len(text) == 2
    intro = [c for c in text if c["section_path"] == "HOW TO USE THIS BOOK"][0]
    monster = [c for c in text if c["section_path"] == "WHAT IS A MONSTER?"][0]
    # The next heading must NOT bleed into the previous section's body.
    assert "WHAT IS A MONSTER?" not in intro["content"]
    assert "Pick up the Starter Set." in intro["content"]
    assert monster["content"].startswith("WHAT IS A MONSTER?")
    assert monster["chunk_ref"] == "/page/0/SectionHeader/1"


def _giants_doc(chapter_html: str) -> dict:
    """A ``chapter -> section -> content`` page: the chapter heading, a section
    heading under it, and a content paragraph whose section_hierarchy carries
    both (the trustworthy ancestry the breadcrumb reads)."""
    return {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": chapter_html,
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/SectionHeader/1",
                        "block_type": "SectionHeader",
                        "html": "<h2>GOLIATHS AND FIRBOLGS</h2>",
                        "section_hierarchy": {
                            "1": "/page/0/SectionHeader/0",
                            "2": "/page/0/SectionHeader/1",
                        },
                    },
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "<p>Two humanoid kinds claim kinship to giants.</p>",
                        "section_hierarchy": {
                            "1": "/page/0/SectionHeader/0",
                            "2": "/page/0/SectionHeader/1",
                        },
                    },
                ],
            )
        ],
        "metadata": {},
    }


def test_to_chunks_nests_section_under_chapter_marker():
    """A section under an explicit CHAPTER heading gets a ``chapter/leaf``
    section_path, while the body is prefixed with (and the header-only chapter
    chunk stores) the leaf name alone."""
    chunks = marker.to_chunks(
        _giants_doc("<h1>CHAPTER 6 GIANTS</h1>"), image_key_prefix="p/"
    )
    by_leaf = {c["section_path"].rsplit("/", 1)[-1]: c for c in chunks}

    goliaths = by_leaf["GOLIATHS AND FIRBOLGS"]
    # Breadcrumb nests the section under its chapter (nav/reader split on "/").
    assert goliaths["section_path"] == "CHAPTER 6 GIANTS/GOLIATHS AND FIRBOLGS"
    # The body is prefixed with the leaf name only, never the full breadcrumb.
    assert goliaths["content"].startswith("GOLIATHS AND FIRBOLGS")
    assert not goliaths["content"].startswith("CHAPTER 6 GIANTS/")
    # The chapter heading (no direct content) keeps its bare top-level path.
    assert by_leaf["CHAPTER 6 GIANTS"]["section_path"] == "CHAPTER 6 GIANTS"


def test_to_chunks_leaves_section_flat_without_chapter_marker():
    """A non-chapter ancestor (a plain section name, a running title, the ToC)
    must NOT be promoted to a chapter: the section stays flat rather than nesting
    under noise."""
    chunks = marker.to_chunks(
        _giants_doc("<h1>MONSTER MANUAL</h1>"), image_key_prefix="p/"
    )
    paths = {c["section_path"] for c in chunks if "image_ref" not in c}
    # "MONSTER MANUAL" is not a CHAPTER/APPENDIX/PART heading, so it never
    # becomes a parent; the section keeps its bare leaf path.
    assert "GOLIATHS AND FIRBOLGS" in paths
    assert not any("/" in p for p in paths)


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
    assert marker.to_chunks(doc, image_key_prefix="p/") == []


# --- section_hierarchy breadcrumb (extraction context) ---------------------


def test_section_hierarchy_path_joins_full_ancestry():
    headers = {
        "toc": "CONTENTS",
        "ch": "Chapter 3: Magic Items",
        "sec": "Armor",
        "leaf": "Armor of Vulnerability",
    }
    hier = {"section_hierarchy": {"1": "toc", "2": "ch", "3": "sec", "4": "leaf"}}
    # CONTENTS furniture dropped; leaf appended once; shallowest-first order.
    assert (
        marker._section_hierarchy_path(hier, headers, "Armor of Vulnerability")
        == "Chapter 3: Magic Items > Armor > Armor of Vulnerability"
    )


def test_section_hierarchy_path_degrades_to_leaf_without_ancestry():
    # No hier_block (header-only run) and no leaf both degrade gracefully.
    assert marker._section_hierarchy_path(None, {}, "Goblin") == "Goblin"
    assert marker._section_hierarchy_path(None, {}, None) is None


def test_to_chunks_emits_full_section_hierarchy():
    """A content block's trustworthy ancestry becomes the section_hierarchy field;
    section_path (2-level) and chunk boundaries are unchanged."""
    doc = {
        "children": [
            _page(
                0,
                [
                    {
                        "id": "/page/0/SectionHeader/0",
                        "block_type": "SectionHeader",
                        "html": "<h1>CHAPTER 1: DRAGONS</h1>",
                        "section_hierarchy": {"1": "/page/0/SectionHeader/0"},
                    },
                    {
                        "id": "/page/0/SectionHeader/1",
                        "block_type": "SectionHeader",
                        "html": "<h2>BRASS DRAGON</h2>",
                        "section_hierarchy": {
                            "1": "/page/0/SectionHeader/0",
                            "2": "/page/0/SectionHeader/1",
                        },
                    },
                    {
                        "id": "/page/0/Text/0",
                        "block_type": "Text",
                        "html": "<p>A talkative metallic dragon.</p>",
                        "section_hierarchy": {
                            "1": "/page/0/SectionHeader/0",
                            "2": "/page/0/SectionHeader/1",
                        },
                    },
                ],
            )
        ],
        "metadata": {},
    }
    chunks = marker.to_chunks(doc, image_key_prefix="p/")
    monster = [c for c in chunks if "talkative" in c["content"]]
    assert len(monster) == 1
    c = monster[0]
    # Full ancestry breadcrumb for extraction context (" > " joined).
    assert c["section_hierarchy"] == "CHAPTER 1: DRAGONS > BRASS DRAGON"
    # section_path stays the 2-level chapter/leaf breadcrumb ("/" joined) that the
    # reader + nav consume; boundaries and chunk_ref are unchanged.
    assert c["section_path"] == "CHAPTER 1: DRAGONS/BRASS DRAGON"
    assert c["chunk_ref"] == "/page/0/SectionHeader/1"
