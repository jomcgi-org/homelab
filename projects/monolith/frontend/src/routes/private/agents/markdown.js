// Markdown rendering for agent turn output, layered over the shared
// escaping renderer in $lib/components/notes/markdown.js.
//
// That renderer is the XSS control for public chat output (ADR 005
// layer 8) and deliberately has no inline-link grammar, so this module
// does NOT extend it. Instead it masks [text](http(s)://...) links with
// NUL sentinels before rendering (NUL cannot appear in the input; it is
// stripped first, the same trick the shared renderer uses for tag
// spans), then swaps the sentinels for anchors built from fully escaped
// parts. javascript:, data:, and every other scheme never match the
// mask, so they stay inert escaped text exactly as before.
//
// The pre-render transforms (h1 downgrade, link masking) are applied
// line by line OUTSIDE fenced code blocks only, so a `# comment` or a
// literal [text](url) inside a fence renders exactly as the agent wrote
// it. The fence open/close patterns mirror the shared renderer's.

import { renderMarkdown } from "$lib/components/notes/markdown.js";

const EMPTY_TITLE_MAP = new Map();
// href charset excludes whitespace, quotes, angle brackets, and `)` so
// the attribute cannot be broken out of; escapeHtml is applied anyway.
const LINK = /\[([^\]]+)\]\((https?:\/\/[^\s)<>"']+)\)/g;
const SENTINEL = (index) => `\x00AGENTLINK${index}\x00`;
const SENTINEL_RESTORE = /\x00AGENTLINK(\d+)\x00/g;
const FENCE_OPEN = /^```\w*\s*$/;
const FENCE_CLOSE = /^```\s*$/;

const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

function mapOutsideFences(text, fn) {
  const out = [];
  let inFence = false;
  for (const line of text.split("\n")) {
    if (!inFence && FENCE_OPEN.test(line)) {
      inFence = true;
      out.push(line);
    } else if (inFence && FENCE_CLOSE.test(line)) {
      inFence = false;
      out.push(line);
    } else {
      out.push(inFence ? line : fn(line));
    }
  }
  return out.join("\n");
}

export function renderAgentMarkdown(text) {
  const cleaned = String(text ?? "")
    .replace(/\x00/g, "")
    // The agent's spoken summary duplicates the answer; it is surfaced
    // separately (turn.voice_summary) and is noise in the transcript.
    // The second replace drops a still-streaming block whose closing
    // tag has not arrived yet, so it never flashes in mid-stream.
    .replace(/<voice>[\s\S]*?<\/voice>/gi, "")
    .replace(/<voice>[\s\S]*$/i, "")
    .trim();

  const links = [];
  const masked = mapOutsideFences(cleaned, (line) =>
    line
      // The shared renderer suppresses h1 (note titles render
      // elsewhere); agent output has no separate title, so keep its
      // top heading.
      .replace(/^# (?=\S)/, "## ")
      .replace(LINK, (_, label, href) => {
        links.push({ label, href });
        return SENTINEL(links.length - 1);
      }),
  );
  const html = renderMarkdown(masked, EMPTY_TITLE_MAP);
  return html.replace(SENTINEL_RESTORE, (match, index) => {
    const link = links[Number(index)];
    if (!link) return "";
    return `<a href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)}</a>`;
  });
}

// Swarm rationale is also rendered by SessionWalkthrough as structured
// testimony. Keep it in the transcript for historical turns, where there is
// no parsed intent, but avoid showing the same trailer twice for swarm turns.
export function stripRationaleTrailer(text, promptIntent) {
  if (promptIntent == null || !text) return text;
  const lines = String(text).split("\n");
  const start = lines.findIndex((line) =>
    /^\s*(?:#{1,6}\s*)?RATIONALE\s*$/i.test(line),
  );
  if (start < 0) return text;
  const nextSection = lines.findIndex(
    (line, index) => index > start && /^\s*#{1,6}\s+\S/.test(line),
  );
  if (nextSection < 0) return lines.slice(0, start).join("\n").trim();
  return lines
    .slice(0, start)
    .concat(lines.slice(nextSection))
    .join("\n")
    .trim();
}
