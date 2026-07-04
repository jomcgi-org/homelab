// Parse a chunk's extracted plain-text content into structural blocks for the
// reader. Marker/LLM extraction emits single-newline-separated lines: bullet
// lines, short ALL-CAPS section headings, and prose. Splitting naively on blank
// lines (the old ChunkReader behaviour) merged single-newline bullets into
// run-on paragraphs and rendered inline headings as body text (e.g. the D&D
// Monster Manual "DUNGEONS" / "THE UNDERDARK" chunk). This groups lines into
// typed blocks the reader renders without {@html}.
//
// Returns an array of blocks:
//   { type: "heading", text }        a short ALL-CAPS line
//   { type: "list", items: [text] }  a run of consecutive bullet lines
//   { type: "para", text }           consecutive prose lines joined with " "
//
// Pure and dependency-free: it unit-tests directly and keeps the Svelte reader
// a trivial per-block render.

// A bullet line opens with •, -, or * followed by whitespace.
const BULLET = /^[•\-*]\s+/;

// Short enough to be a heading rather than a wrapped sentence.
const HEADING_MAX = 60;

function isBullet(line) {
  return BULLET.test(line);
}

// An ALL-CAPS line: has at least one letter and no lowercase letter, and is
// short. Digits and punctuation are allowed (e.g. "CHAPTER 1").
function isHeading(line) {
  if (line.length === 0 || line.length >= HEADING_MAX) return false;
  return /[A-Z]/.test(line) && !/[a-z]/.test(line);
}

export function renderChunk(content) {
  const blocks = [];
  if (!content) return blocks;

  let para = [];
  let list = [];

  const flushPara = () => {
    if (para.length) {
      blocks.push({ type: "para", text: para.join(" ") });
      para = [];
    }
  };
  const flushList = () => {
    if (list.length) {
      blocks.push({ type: "list", items: list });
      list = [];
    }
  };

  for (const raw of content.split("\n")) {
    const line = raw.trim();
    if (line === "") {
      // A blank line closes whatever grouping is open.
      flushList();
      flushPara();
      continue;
    }
    if (isBullet(line)) {
      flushPara();
      list.push(line.replace(BULLET, ""));
      continue;
    }
    // Any non-bullet line ends a run of bullets.
    flushList();
    if (isHeading(line)) {
      flushPara();
      blocks.push({ type: "heading", text: line });
      continue;
    }
    para.push(line);
  }
  flushList();
  flushPara();
  return blocks;
}
