/**
 * Agent output as blocks the view paints. Prompts, replies and issue briefs
 * are markdown written by other people and other models, so none of it may
 * reach the page as markup: marked tokenises it and this module reduces the
 * tokens to a small vocabulary of plain objects, which Markdown.svelte renders
 * through text nodes only. Raw HTML, inline or block, becomes visible text,
 * and a link keeps its href only when the scheme is one a reader can follow.
 */
import { marked } from "marked";

const SAFE_HREF = /^(?:https?:|mailto:|\/|#|\.\.?\/)/i;

/** The schemes a link keeps. Anything else renders as text with no href. */
export function safeHref(href) {
  const value = (href ?? "").trim();
  return SAFE_HREF.test(value) ? value : null;
}

function inlineOf(tokens) {
  const runs = [];
  for (const token of tokens ?? []) {
    switch (token.type) {
      case "text":
      case "escape":
        // A tight list item arrives as a `text` token carrying its own
        // children; a leaf text token carries none.
        if (token.tokens?.length) runs.push(...inlineOf(token.tokens));
        else runs.push({ text: token.text });
        break;
      case "codespan":
        runs.push({ code: token.text });
        break;
      case "strong":
        runs.push({ strong: inlineOf(token.tokens) });
        break;
      case "em":
        runs.push({ em: inlineOf(token.tokens) });
        break;
      case "del":
        runs.push({ del: inlineOf(token.tokens) });
        break;
      case "link":
        runs.push({
          link: safeHref(token.href),
          inline: inlineOf(token.tokens),
        });
        break;
      case "image":
        runs.push({ text: token.text || token.href || "" });
        break;
      case "br":
        runs.push({ br: true });
        break;
      case "checkbox":
        // A task list's box arrives inline ahead of the item's text.
        runs.push({ box: Boolean(token.checked) });
        break;
      case "html":
      default:
        runs.push({ text: token.raw ?? token.text ?? "" });
    }
  }
  return runs;
}

function blocksOf(tokens) {
  const blocks = [];
  // In a tight list item the task box is a block-level token ahead of the
  // item's text; it is carried into the next block so the two share a line.
  let box = null;
  const lead = (inline) => {
    if (box == null) return inline;
    const runs = [{ box }, ...inline];
    box = null;
    return runs;
  };
  for (const token of tokens ?? []) {
    switch (token.type) {
      case "space":
      case "def":
        break;
      case "checkbox":
        box = Boolean(token.checked);
        break;
      case "heading":
        blocks.push({
          type: "heading",
          depth: token.depth,
          inline: inlineOf(token.tokens),
        });
        break;
      case "paragraph":
        blocks.push({
          type: "paragraph",
          inline: lead(inlineOf(token.tokens)),
        });
        break;
      case "text":
        // Inside a tight list item, a run of text that is not a paragraph.
        blocks.push({
          type: "text",
          inline: lead(inlineOf(token.tokens ?? [token])),
        });
        break;
      case "code":
        blocks.push({ type: "code", lang: token.lang || "", text: token.text });
        break;
      case "blockquote":
        blocks.push({ type: "quote", blocks: blocksOf(token.tokens) });
        break;
      case "hr":
        blocks.push({ type: "rule" });
        break;
      case "list":
        blocks.push({
          type: "list",
          ordered: Boolean(token.ordered),
          start: token.ordered && token.start !== "" ? Number(token.start) : 1,
          items: (token.items ?? []).map((item) => ({
            task: Boolean(item.task),
            checked: Boolean(item.checked),
            blocks: blocksOf(item.tokens),
          })),
        });
        break;
      case "table":
        blocks.push({
          type: "table",
          align: token.align ?? [],
          header: (token.header ?? []).map((cell) => inlineOf(cell.tokens)),
          rows: (token.rows ?? []).map((row) =>
            row.map((cell) => inlineOf(cell.tokens)),
          ),
        });
        break;
      case "html":
      default:
        blocks.push({
          type: "paragraph",
          inline: [{ text: token.raw ?? token.text ?? "" }],
        });
    }
  }
  return blocks;
}

/** The markdown as blocks. Never throws: broken input is still someone's text. */
export function markdownBlocks(text) {
  const source = typeof text === "string" ? text : "";
  if (!source.trim()) return [];
  try {
    return blocksOf(marked.lexer(source, { gfm: true }));
  } catch {
    return [{ type: "paragraph", inline: [{ text: source }] }];
  }
}

/**
 * The first line of a reply as plain words, for a row that previews it: the
 * markdown marks a reader would otherwise see as noise are dropped.
 */
export function plainPreview(text, width = 160) {
  const source = typeof text === "string" ? text : "";
  const flat = source
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/[*~`>]+/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return flat.length <= width ? flat : `${flat.slice(0, width - 1)}…`;
}
