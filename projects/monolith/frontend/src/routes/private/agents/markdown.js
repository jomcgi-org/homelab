// Markdown rendering for agent turn output, layered over the shared
// escaping renderer in $lib/components/notes/markdown.js.
//
// That renderer is the XSS control for public chat output (ADR 005
// layer 8) and deliberately has no inline-link grammar, so this module
// does NOT extend it. Instead it masks [text](https://...) links with
// NUL sentinels before rendering (NUL cannot appear in the input; it is
// stripped first, the same trick the shared renderer uses for tag
// spans), then swaps the sentinels for anchors built from fully escaped
// parts. javascript:, data:, and every other scheme never match the
// mask, so they stay inert escaped text exactly as before.

import { renderMarkdown } from "$lib/components/notes/markdown.js";

const EMPTY_TITLE_MAP = new Map();
// href charset excludes whitespace, quotes, angle brackets, and `)` so
// the attribute cannot be broken out of; escapeHtml is applied anyway.
const LINK = /\[([^\]]+)\]\((https?:\/\/[^\s)<>"']+)\)/g;
const SENTINEL = (index) => `\x00AGENTLINK${index}\x00`;
const SENTINEL_RESTORE = /\x00AGENTLINK(\d+)\x00/g;

const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

export function renderAgentMarkdown(text) {
  const cleaned = String(text ?? "")
    .replace(/\x00/g, "")
    // The agent's spoken summary duplicates the answer; it is surfaced
    // separately (turn.voice_summary) and is noise in the transcript.
    .replace(/<voice>[\s\S]*?<\/voice>/gi, "")
    // The shared renderer suppresses h1 (note titles render elsewhere);
    // agent output has no separate title, so keep its top heading.
    .replace(/^# (?=\S)/gm, "## ")
    .trim();

  const links = [];
  const masked = cleaned.replace(LINK, (_, label, href) => {
    links.push({ label, href });
    return SENTINEL(links.length - 1);
  });
  const html = renderMarkdown(masked, EMPTY_TITLE_MAP);
  return html.replace(SENTINEL_RESTORE, (match, index) => {
    const link = links[Number(index)];
    if (!link) return "";
    return `<a href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)}</a>`;
  });
}
