// Pure fold turning the flat /books/{id}/sections rows into a two-level tree
// for the book reader's collapsible TOC.
//
// Each row's section_path is either a bare leaf title ("Introduction", no
// slash: pre-existing top-level rows, forewords, appendices) or a two-level
// "Chapter/Leaf" path. Splitting is done on the FIRST slash only, so a leaf
// title that itself contains a slash (e.g. "Traps/Pits") stays intact as one
// child rather than being sliced again.
//
// Grouping is a single left-to-right fold over reading order, not a global
// group-by: consecutive rows sharing the same chapter name merge into one
// chapter node, but if a chapter name reappears later after other chapters
// have interleaved, that later run starts a NEW node with the same title.
// This deliberately mirrors the backend's own reading-order semantics
// elsewhere in the corpus (first-appearance wins) rather than deduplicating
// globally, which would silently reorder content that appears twice under
// the same heading (rare, but seen in some anthology books).
export function buildSectionTree(flatSections) {
  const tree = [];
  let currentChapter = null; // the open chapter node, if any
  let currentChapterTitle = null;

  for (const section of flatSections ?? []) {
    const path = section.section_path ?? "";
    const slashIndex = path.indexOf("/");

    if (slashIndex === -1) {
      // Top-level leaf: closes any open chapter run.
      tree.push({ title: section.title, section, children: [] });
      currentChapter = null;
      currentChapterTitle = null;
      continue;
    }

    const chapterTitle = path.slice(0, slashIndex);
    const leafTitle = path.slice(slashIndex + 1);

    if (!currentChapter || currentChapterTitle !== chapterTitle) {
      currentChapter = { title: chapterTitle, section: null, children: [] };
      currentChapterTitle = chapterTitle;
      tree.push(currentChapter);
    }
    currentChapter.children.push({
      title: leafTitle,
      section,
      children: [],
    });
  }

  return tree;
}
