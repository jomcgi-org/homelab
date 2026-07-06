#!/usr/bin/env bash
# Bake the scroll-story asset bundle for the public Grimoire landing.
#
# Reproducible curation: the landing's "From Scan to Sage" explainer is driven
# by one showcase page. If a better candidate page is found later, re-run this
# script and commit the regenerated data/ directory; nothing else changes.
#
# Usage:
#   bake-scrollstory.sh <book_id> <page> <pdf_path>
#
#   book_id   grimoire.book id, e.g. lost-mine-of-phandelver
#   page      0-based marker page index (matches chunk_ref /page/N/...)
#   pdf_path  local PDF of the book (page N+1 in 1-based pdftoppm terms)
#
# Requires: kubectl (cluster read access), pdftoppm, cwebp, python3.
# Reads Postgres via the monolith-pg pod and the marker layout output from
# SeaweedFS; renders the page scan from the local PDF. Writes:
#   data/page.webp   the page scan (max 1200px wide)
#   data/story.js    chunks, bboxes, entities, mentions, edges, corpus totals
#
# The chat transcript is deliberately NOT baked here: it is hand-curated in
# data/transcript.js (the demo question changes with the page).
set -euo pipefail

BOOK="${1:?usage: bake-scrollstory.sh <book_id> <page> <pdf_path>}"
PAGE="${2:?missing 0-based page index}"
PDF="${3:?missing local pdf path}"

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/data"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"

echo "▶ exporting chunks/entities/edges from Postgres (book=$BOOK page=$PAGE)"
kubectl exec -i -n monolith monolith-pg-1 -c postgres -- psql -U postgres -d monolith -At \
	-v book="$BOOK" -v page="$PAGE" <<'SQL' >"$TMP/export.json"
WITH pc AS (
  SELECT c.* FROM grimoire.knowledge_chunk c
  WHERE c.book_id = :'book' AND c.chunk_ref LIKE '/page/' || :'page' || '/%'
), ents AS (
  SELECT DISTINCT e.id, e.name, e.entity_type
  FROM pc JOIN grimoire.chunk_entity_mention m ON m.chunk_id = pc.id
  JOIN grimoire.entity e ON e.id = m.entity_id
)
SELECT json_build_object(
  'book', :'book',
  'page', (:'page')::int,
  'chunks', (SELECT json_agg(json_build_object(
      'id', id, 'ref', chunk_ref, 'section', section_path, 'content', content)
      ORDER BY chunk_ref) FROM pc),
  'entities', (SELECT json_agg(json_build_object(
      'id', id, 'name', name, 'type', entity_type) ORDER BY name) FROM ents),
  'mentions', (SELECT json_agg(json_build_object(
      'chunk', m.chunk_id, 'entity', m.entity_id, 'text', m.mention_text)
      ORDER BY m.chunk_id, m.entity_id)
      FROM pc JOIN grimoire.chunk_entity_mention m ON m.chunk_id = pc.id),
  'edges', (SELECT json_agg(json_build_object(
      'from', r.from_entity_id, 'to', r.to_entity_id, 'type', r.rel_type)
      ORDER BY r.from_entity_id, r.to_entity_id, r.rel_type)
      FROM grimoire.relationship r
      WHERE r.from_entity_id IN (SELECT id FROM ents)
        AND r.to_entity_id IN (SELECT id FROM ents)),
  'corpus', json_build_object(
    'books', (SELECT count(*) FROM grimoire.book),
    'chunks', (SELECT count(*) FROM grimoire.knowledge_chunk),
    'entities', (SELECT count(*) FROM grimoire.entity),
    'edges', (SELECT count(*) FROM grimoire.relationship),
    'byType', (SELECT json_object_agg(entity_type, n)
               FROM (SELECT entity_type, count(*) AS n
                     FROM grimoire.entity GROUP BY 1) t)
  )
);
SQL

echo "▶ fetching marker layout from SeaweedFS"
kubectl exec -n seaweedfs seaweedfs-filer-0 -c seaweedfs -- sh -c \
	"wget -qO- http://seaweedfs-filer-client.seaweedfs:8888/buckets/grimoire/books/$BOOK/raw/output.json.gz | base64 -w0" |
	python3 -c "import base64,sys,gzip; sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))" \
		>"$TMP/marker.json"

echo "▶ rendering page scan from $PDF"
pdftoppm -png -f "$((PAGE + 1))" -l "$((PAGE + 1))" -r 150 "$PDF" "$TMP/scan"
SCAN="$(ls "$TMP"/scan-*.png)"
cwebp -quiet -q 80 -resize 1200 0 "$SCAN" -o "$OUT/page.webp"

echo "▶ writing data/story.js"
python3 - "$TMP/export.json" "$TMP/marker.json" "$OUT/story.js" "$BOOK" "$PAGE" <<'PY'
import json
import sys

export_path, marker_path, out_path, book, page_s = sys.argv[1:6]
page_no = int(page_s)

export = json.load(open(export_path))
marker = json.load(open(marker_path))
page = marker["children"][page_no]
pw, ph = page["bbox"][2], page["bbox"][3]

KIND = {
    "SectionHeader": "header",
    "Text": "text",
    "Picture": "art",
    "Figure": "art",
    "Caption": "caption",
    "Table": "aside",
    "TextInlineMath": "text",
    "ListItem": "text",
}

chunk_refs = {c["ref"] for c in export["chunks"] or []}
bboxes = []
current = None
for block in page.get("children") or []:
    btype = block.get("block_type")
    if btype in ("PageFooter", "PageHeader"):
        continue
    bid = block["id"]
    if bid in chunk_refs:
        current = bid
    x0, y0, x1, y1 = block["bbox"]
    bboxes.append(
        {
            "id": bid,
            "kind": KIND.get(btype, "text"),
            # fractional page coords so any render size works
            "x": round(x0 / pw, 4),
            "y": round(y0 / ph, 4),
            "w": round((x1 - x0) / pw, 4),
            "h": round((y1 - y0) / ph, 4),
            # None for blocks whose chunk starts on an earlier page (the
            # extraction carries sections across pages); the story fades
            # those out instead of flying them.
            "chunkId": current,
        }
    )

story = {
    "source": {"book": book, "page": page_no, "aspect": round(pw / ph, 4)},
    "bboxes": bboxes,
    "chunks": export["chunks"] or [],
    "entities": export["entities"] or [],
    "mentions": export["mentions"] or [],
    "edges": export["edges"] or [],
    "corpus": export["corpus"],
}

banner = (
    "// GENERATED by bake-scrollstory.sh - do not hand-edit.\n"
    f"// Regenerate: ./bake-scrollstory.sh {book} {page_no} <local-pdf>\n"
    "// The chat transcript lives in transcript.js (hand-curated).\n"
)
body = "".join(
    f"export const {key} = {json.dumps(value, indent=2, ensure_ascii=False)};\n\n"
    for key, value in story.items()
)
with open(out_path, "w") as f:
    f.write(banner + "\nimport pageImage from \"./page.webp\";\n\n")
    f.write("export const image = pageImage;\n\n")
    f.write(body)
print(
    f"  {len(bboxes)} bboxes, {len(story['chunks'])} chunks, "
    f"{len(story['entities'])} entities, {len(story['edges'])} edges"
)
PY

echo "✔ baked $OUT/page.webp and $OUT/story.js"
