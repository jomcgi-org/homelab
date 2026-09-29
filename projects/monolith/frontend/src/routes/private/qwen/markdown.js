import { Marked } from "marked";

const escapeHtml = (text) =>
  text.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );

function safeUrl(href, protocols) {
  try {
    return protocols.includes(
      new URL(href, "https://markdown.invalid/").protocol,
    );
  } catch {
    return false;
  }
}

// Escape raw HTML and every attribute. Only web and email links are clickable;
// images accept web URLs, including relative paths.
const markdown = new Marked({
  renderer: {
    html: ({ text }) => escapeHtml(text),
    link({ href, title, tokens }) {
      const label = this.parser.parseInline(tokens);
      if (!safeUrl(href, ["http:", "https:", "mailto:"])) return label;
      const tooltip = title ? ` title="${escapeHtml(title)}"` : "";
      return `<a href="${escapeHtml(href)}"${tooltip}>${label}</a>`;
    },
    image({ href, title, text }) {
      if (!safeUrl(href, ["http:", "https:"])) return escapeHtml(text);
      const tooltip = title ? ` title="${escapeHtml(title)}"` : "";
      return `<img src="${escapeHtml(href)}" alt="${escapeHtml(text)}"${tooltip}>`;
    },
  },
});

export const renderMarkdown = (text) => markdown.parse(text ?? "");
