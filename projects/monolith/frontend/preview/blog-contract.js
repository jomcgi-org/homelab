/**
 * The first fixture contract is the public blog server-load result. It has no
 * network API. Compare it with the actual loader at this PR's commit in tests.
 * @typedef {{id: string, text: string, depth: number}} Heading
 * @typedef {{slug: string, title: string, date: string, summary: string,
 * tags: string[], html: string, preamble: string, sections: string[],
 * toc: (Heading & {children: Heading[]})[]}} BlogPage
 */
const keys = (value, expected) =>
  Object.keys(value).sort().join() === [...expected].sort().join();
const heading = (value) =>
  value &&
  typeof value.id === "string" &&
  typeof value.text === "string" &&
  Number.isInteger(value.depth);
/** @returns {asserts value is BlogPage} */
export function assertBlogPage(value) {
  if (
    !value ||
    !keys(value, [
      "slug",
      "title",
      "date",
      "summary",
      "tags",
      "html",
      "preamble",
      "sections",
      "toc",
    ]) ||
    !["slug", "title", "date", "summary", "html", "preamble"].every(
      (key) => typeof value[key] === "string",
    ) ||
    !["tags", "sections"].every(
      (key) =>
        Array.isArray(value[key]) &&
        value[key].every((item) => typeof item === "string"),
    ) ||
    !Array.isArray(value.toc) ||
    !value.toc.every(
      (item) =>
        heading(item) &&
        keys(item, ["id", "text", "depth", "children"]) &&
        item.depth === 2 &&
        Array.isArray(item.children) &&
        item.children.every(
          (child) =>
            heading(child) &&
            keys(child, ["id", "text", "depth"]) &&
            child.depth === 3,
        ),
    )
  ) {
    throw new TypeError(
      "Blog page fixture no longer matches its server-load contract",
    );
  }
}
