"""Transform a Marker (Datalab) JSON extraction into grimoire chunk NDJSON.

Marker (https://github.com/datalab-to/marker) renders a PDF to a ``JSONOutput``:
a recursive block tree we consume by its documented *output contract* rather
than by depending on the ``marker-pdf`` package (which pulls the full torch +
surya inference stack, absurd for reading JSON we already have). The contract,
mirrored from ``marker.renderers.json`` and treated as our stable interface:

    JSONBlockOutput = {
        "id": str,                       # e.g. "/page/192/SectionHeader/1"
        "block_type": str,               # Page | Text | SectionHeader | Picture | Table | ...
        "html": str,                     # block content as HTML
        "children": list[JSONBlockOutput] | None,
        "section_hierarchy": {level: block_id} | None,  # running header stack
        "images": {filename: base64} | None,            # Picture blocks only
    }
    JSONOutput = {"children": [Page blocks], "block_type": "Document", "metadata": {...}}

Chunking (ADR 012): text chunks are contiguous runs of blocks sharing the same
section *name*. ``section_hierarchy`` maps heading-level -> the governing
SectionHeader's block **id** (not its text); the deepest entry is the nearest
preceding header, but the level numbering is noisy (stale ancestors leak in) and
a monster's lore and stat block often sit under two separate same-named headers,
so we resolve each block to its nearest non-continuation header *name* and group
adjacent blocks that share it. That keeps each monster (lore + stat block +
ACTIONS/REACTIONS, which are continuation sub-headers folded into the parent) in
one chunk without depending on the noisy level ancestry.

Two chunk kinds, both emitted as ``{chunk_ref, content, section_path, image_ref?}``
NDJSON lines that ``ingest.parse_manifest_lines`` consumes:

  - text chunk: a run's blocks, HTML-stripped and joined in document order, with
    the section name prepended as a title line. ``image_ref`` omitted.
  - image chunk: one per Picture block. ``content`` is Marker's LLM-generated
    caption (the ``img-description``), falling back to the ``alt`` text;
    ``image_ref`` is the full ``s3://`` URI of the cropped image, so the app can
    render the picture on retrieval. The caption text still flows through
    embedding + entity extraction like any other chunk.

Pure stdlib (json/re/html) so it runs identically on a workstation, in CI, and
in-cluster. Import the functions for tests; run the file directly as a CLI.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
from collections.abc import Iterator

# Block types that are page furniture, not content: excluded from text chunks.
_NON_CONTENT = frozenset({"Page", "PageFooter", "PageHeader"})

# 5e stat blocks nest these as sub-headers under a monster, so keying on the
# deepest leaf splits a creature's actions off from its stat block (36% of text
# chunks on the Monster Manual). Attribute their blocks to the nearest
# non-continuation ancestor instead, reuniting the monster's stat block, traits,
# and actions in one chunk. Compared uppercased/stripped against header text.
_CONTINUATION_HEADERS = frozenset(
    {
        "ACTIONS",
        "BONUS ACTIONS",
        "REACTIONS",
        "LEGENDARY ACTIONS",
        "LAIR ACTIONS",
        "REGIONAL EFFECTS",
    }
)

_TAG_RE = re.compile(r"<[^>]+>")
# Tags whose boundary should become a newline when flattening HTML to text.
_BLOCK_BREAK_RE = re.compile(
    r"</(?:p|div|h[1-6]|tr|li|ul|ol|table|caption)>|<br\s*/?>", re.IGNORECASE
)
_CELL_BREAK_RE = re.compile(r"</(?:td|th)>", re.IGNORECASE)
_WS_RUN_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
_IMG_ALT_RE = re.compile(r'<img\b[^>]*\balt="([^"]*)"', re.IGNORECASE)
# Marker nests the LLM caption as <div class="img-description" ...><p>CAPTION</p>
# <div class="img-alt">ALT</div></div>. Capture up to the nested img-alt (no
# closing </div> separates them); fall back to the description's own close.
_IMG_DESC_RE = re.compile(
    r'<div class="img-description"[^>]*>(.*?)<div class="img-alt"',
    re.IGNORECASE | re.DOTALL,
)
_IMG_DESC_FALLBACK_RE = re.compile(
    r'<div class="img-description"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)


def html_to_text(html: str) -> str:
    """Flatten a block's HTML to readable plain text.

    Table cells become space-separated, block-level tags become newlines, so a
    stat table keeps its values in reading order without HTML noise polluting
    embeddings. Entities are unescaped and runs of whitespace collapsed.
    """
    if not html:
        return ""
    text = _CELL_BREAK_RE.sub(" ", html)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = _WS_RUN_RE.sub(" ", text)
    # Trim trailing spaces on each line, then squeeze blank-line runs.
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def iter_blocks(doc: dict) -> Iterator[dict]:
    """Yield every block in the tree, parents before children, in document order."""
    stack = list(reversed(doc.get("children") or []))
    while stack:
        block = stack.pop()
        if not isinstance(block, dict):
            continue
        yield block
        children = block.get("children") or []
        stack.extend(reversed(children))


def build_header_text(doc: dict) -> dict[str, str]:
    """Map every SectionHeader block id -> its plain-text heading."""
    headers: dict[str, str] = {}
    for block in iter_blocks(doc):
        if block.get("block_type") == "SectionHeader":
            headers[block.get("id", "")] = html_to_text(block.get("html"))
    return headers


def leaf_section_id(block: dict) -> str | None:
    """The deepest (nearest) SectionHeader id governing this block, or None."""
    sh = block.get("section_hierarchy") or {}
    if not sh:
        return None
    deepest = max(sh, key=lambda k: int(k))
    return sh[deepest]


def effective_section_id(block: dict, headers: dict[str, str]) -> str | None:
    """Grouping key: nearest ancestor header that is not a stat-block continuation.

    Walks the section_hierarchy from deepest to shallowest and returns the first
    id whose header text is not in ``_CONTINUATION_HEADERS`` (so ACTIONS /
    REACTIONS / LEGENDARY ACTIONS blocks merge into their monster). Falls back to
    the deepest id if every level is a continuation header.
    """
    sh = block.get("section_hierarchy") or {}
    if not sh:
        return None
    for level in sorted(sh, key=lambda k: int(k), reverse=True):
        sid = sh[level]
        if headers.get(sid, "").strip().upper() not in _CONTINUATION_HEADERS:
            return sid
    return sh[max(sh, key=lambda k: int(k))]


def _page_of(block: dict) -> str:
    """Fallback grouping key for blocks with no section: their page id prefix."""
    bid = block.get("id", "")
    m = re.match(r"(/page/\d+)/", bid)
    return f"{m.group(1)}/_nosection" if m else "/_nosection"


def parse_image_block(html: str) -> tuple[str, str, str]:
    """From a Picture block's html, return (src_filename, alt, caption)."""
    src_m = _IMG_SRC_RE.search(html)
    alt_m = _IMG_ALT_RE.search(html)
    desc_m = _IMG_DESC_RE.search(html) or _IMG_DESC_FALLBACK_RE.search(html)
    src = src_m.group(1) if src_m else ""
    alt = alt_m.group(1) if alt_m else ""
    caption = html_to_text(desc_m.group(1)) if desc_m else ""
    return _html.unescape(src), _html.unescape(alt), caption


def _image_content(alt: str, caption: str) -> str:
    """Image-chunk content: prefer the rich LLM caption, fall back to alt text."""
    return caption.strip() or alt.strip()


def to_chunks(doc: dict, *, book_id: str, image_key_prefix: str) -> list[dict]:
    """Convert a Marker ``JSONOutput`` dict to grimoire chunk dicts.

    ``image_key_prefix`` is prepended to each image's src filename to form its
    ``image_ref`` (our S3 key), e.g. ``"books/monster-manual/raw/img/"``.

    Text chunks are contiguous runs of blocks sharing the same section name (in
    document order): Marker emits a monster's lore and its stat block under two
    separate same-named SectionHeaders, so grouping by run-of-same-name (not by
    header id) keeps each monster whole in one chunk. A new, differently-named
    header breaks the run. Image chunks are one per Picture and never break a
    run. Empty-content chunks are dropped (the loader requires non-empty content).
    """
    headers = build_header_text(doc)
    text_chunks: list[dict] = []
    image_chunks: list[dict] = []
    run: dict | None = None  # {key, chunk_ref, section_path, parts}

    def flush() -> None:
        if not run or not run["parts"]:
            return
        body = "\n\n".join(run["parts"]).strip()
        if not body:
            return
        sp = run["section_path"]
        # The extractor sees only `content`, so the section/monster name must
        # live in the text itself (not just section_path) for the entity to be
        # named reliably. Prepend it as a title line unless already leading.
        if sp and not body.upper().startswith(sp.upper()):
            body = f"{sp}\n\n{body}"
        text_chunks.append(
            {"chunk_ref": run["chunk_ref"], "content": body, "section_path": sp}
        )

    for block in iter_blocks(doc):
        btype = block.get("block_type")
        if btype in _NON_CONTENT:
            continue

        if btype == "Picture":
            src, alt, caption = parse_image_block(block.get("html") or "")
            content = _image_content(alt, caption)
            if content:
                image_chunks.append(
                    {
                        "chunk_ref": block.get("id", ""),
                        "content": content,
                        "section_path": headers.get(
                            effective_section_id(block, headers) or ""
                        )
                        or None,
                        "image_ref": (image_key_prefix + src) if src else None,
                    }
                )
            continue

        text = html_to_text(block.get("html"))
        if not text:
            continue
        sid = effective_section_id(block, headers)
        section_path = headers.get(sid) if sid else None
        # Run key is the section *name* so two adjacent same-named sections
        # merge; nameless blocks fall back to their page so they do not all
        # collapse into one giant None run.
        key = section_path or _page_of(block)
        if run is None or run["key"] != key:
            flush()
            run = {
                "key": key,
                "chunk_ref": sid or _page_of(block),
                "section_path": section_path or None,
                "parts": [],
            }
        run["parts"].append(text)
    flush()

    return text_chunks + image_chunks


def write_ndjson(chunks: list[dict], out) -> int:
    """Write chunk dicts as NDJSON (image_ref omitted when None). Returns count."""
    n = 0
    for c in chunks:
        line = {k: v for k, v in c.items() if v is not None}
        out.write(json.dumps(line, ensure_ascii=False) + "\n")
        n += 1
    return n


def convert_file(
    input_json: str, *, book_id: str, bucket: str = "grimoire"
) -> list[dict]:
    with open(input_json, encoding="utf-8") as f:
        doc = json.load(f)
    # Full s3:// URI so image_ref is the complete, self-describing location
    # imgproxy consumes directly (s3://bucket/key), no bucket-prefixing needed
    # at render time.
    image_key_prefix = f"s3://{bucket}/books/{book_id}/raw/img/"
    return to_chunks(doc, book_id=book_id, image_key_prefix=image_key_prefix)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="Marker output.json")
    ap.add_argument("--book-id", required=True, help="book id (S3 path + source_book)")
    ap.add_argument("--bucket", default="grimoire", help="S3 bucket for image_ref URIs")
    ap.add_argument(
        "-o", "--out", default="-", help="output NDJSON path ('-' for stdout)"
    )
    args = ap.parse_args(argv)

    chunks = convert_file(args.input, book_id=args.book_id, bucket=args.bucket)
    if args.out == "-":
        n = write_ndjson(chunks, sys.stdout)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            n = write_ndjson(chunks, f)
    texts = sum(1 for c in chunks if c.get("image_ref") is None)
    print(
        f"wrote {n} chunks ({texts} text, {n - texts} image) to {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
