// Pure fold turning the flat /books/{id}/sections rows into an arbitrary-depth
// tree for the book reader's collapsible TOC.
//
// The backend now emits one row per DISTINCT node in the section-hierarchy
// breadcrumb (chapter, sub-section, sub-sub-section, ...), not one row per
// raw section_path leaf: a book whose section_path column fragments into
// thousands of near-duplicate paths collapses back down to its real chapter
// count once grouped by the shared breadcrumb prefix. Each row carries
// `depth` (0 = top-level) and `parent_path` (the parent's own `section_path`
// breadcrumb string, or null at depth 0), so nesting is a single left-to-right
// walk keyed on parent_path rather than string-splitting section_path.
//
// Rows arrive in reading order (first appearance by seq), and a parent row
// always precedes its children (the backend emits every ancestor node before
// the chunk that deepens into it). A node with no directly-tagged chunks of
// its own (e.g. a chapter heading with only sub-sectioned content) still gets
// a `first_chunk_id` -- the earliest chunk anywhere beneath it -- so every row
// is clickable.
//
// Each row also carries `raw_section_paths`: the distinct chunk-level
// section_path values rolled up into that node. The reader's scroll-driven
// activeSectionPath is one of those raw values, not this node's synthesized
// breadcrumb string, so callers match on membership in that list rather than
// equality on section_path (see ChaptersNav.svelte).
export function buildSectionTree(flatSections) {
  const tree = [];
  const byPath = new Map(); // section_path -> tree node, for parent lookup

  for (const section of flatSections ?? []) {
    const node = { title: section.title, section, children: [] };
    byPath.set(section.section_path, node);

    const parentPath = section.parent_path ?? null;
    const parent = parentPath ? byPath.get(parentPath) : null;
    if (parent) {
      parent.children.push(node);
    } else {
      tree.push(node);
    }
  }

  return tree;
}
