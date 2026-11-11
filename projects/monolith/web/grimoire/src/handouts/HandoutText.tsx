import type { ReactNode } from "react";
import { Heading } from "@astryxdesign/core/Heading";
import "./handouts.css";

type HeadingLevel = 1 | 2 | 3 | 4 | 5 | 6;

interface HandoutTextProps {
  markdown: string | null | undefined;
  /** The level a `##` heading renders at; `###` is one deeper. */
  headingLevel?: HeadingLevel;
}

/**
 * A handout's text. The DM writes a small markdown subset: `##` and `###`
 * headings, dash and numbered lists, quotes, fenced code, pipe tables, bold,
 * italic and inline code. It is parsed into React elements, so the source only
 * ever reaches the page as text: no HTML string is built or injected, and the
 * grammar has no links or images to carry a URL.
 */
export function HandoutText({ markdown, headingLevel = 4 }: HandoutTextProps) {
  const blocks = parseBlocks(String(markdown ?? ""), headingLevel);
  if (blocks.length === 0) return null;
  return <div className="handout-text">{blocks}</div>;
}

const TAGS = ["strong", "em", "code"] as const;
type Tag = (typeof TAGS)[number];

// Private-use characters mark where an inline element opens and closes while
// the emphasis rules run over the line as plain text.
const OPEN: Record<Tag, string> = {
  strong: "",
  em: "",
  code: "",
};
const CLOSE: Record<Tag, string> = {
  strong: "",
  em: "",
  code: "",
};
const MARKERS = /[--]/g;
const tagOf = (marks: Record<Tag, string>, char: string) =>
  TAGS.find((tag) => marks[tag] === char);

const wrap = (tag: Tag) => `${OPEN[tag]}$1${CLOSE[tag]}`;

function mark(line: string): string {
  return (
    line
      .replace(/\*\*([^*]+)\*\*/g, wrap("strong"))
      .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, wrap("em"))
      .replace(/`([^`]+)`/g, wrap("code"))
      // [[target|display]] has nothing to link to in a handout: show the name.
      .replace(/\[\[([^\]]+)\]\]/g, (_match, inner: string) => {
        const pipe = inner.indexOf("|");
        return pipe >= 0 ? inner.slice(pipe + 1).trim() : inner;
      })
  );
}

interface Frame {
  tag: Tag | null;
  children: ReactNode[];
}

function inline(line: string): ReactNode[] {
  const root: Frame = { tag: null, children: [] };
  const stack: Frame[] = [root];
  let text = "";
  const top = () => stack[stack.length - 1]!;
  const flush = () => {
    if (text) top().children.push(text);
    text = "";
  };
  const close = () => {
    const frame = stack.pop()!;
    const Element = frame.tag!;
    const parent = top();
    parent.children.push(
      <Element key={parent.children.length}>{frame.children}</Element>,
    );
  };
  for (const char of mark(line)) {
    const opened = tagOf(OPEN, char);
    const closed = tagOf(CLOSE, char);
    if (opened) {
      flush();
      stack.push({ tag: opened, children: [] });
    } else if (closed) {
      flush();
      // Overlapping emphasis closes whatever it crossed; a stray close is dropped.
      const at = stack.findLastIndex((frame) => frame.tag === closed);
      if (at > 0) while (stack.length > at) close();
    } else text += char;
  }
  flush();
  while (stack.length > 1) close();
  return root.children;
}

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/;
// Stands in for a pipe inside [[target|display]] while a row is split.
const HELD_PIPE = "";

function cells(row: string): string[] {
  return row
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .replace(/\[\[([^\]]+)\]\]/g, (link) => link.replaceAll("|", HELD_PIPE))
    .split("|")
    .map((cell) => cell.trim().replaceAll(HELD_PIPE, "|"));
}

const deeper = (level: HeadingLevel, by: number) =>
  Math.min(6, level + by) as HeadingLevel;

function parseBlocks(source: string, headingLevel: HeadingLevel): ReactNode[] {
  const lines = source
    .replace(MARKERS, "")
    .replaceAll(HELD_PIPE, "")
    .split("\n");
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: ReactNode[][] } | null = null;
  let quote: ReactNode[][] | null = null;

  const flushList = () => {
    if (!list) return;
    const List = list.ordered ? "ol" : "ul";
    blocks.push(
      <List key={blocks.length}>
        {list.items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </List>,
    );
    list = null;
  };
  const flushQuote = () => {
    if (!quote) return;
    blocks.push(
      <blockquote key={blocks.length}>
        {quote.map((line, index) => (
          <span key={index}>
            {index > 0 && "\n"}
            {line}
          </span>
        ))}
      </blockquote>,
    );
    quote = null;
  };
  const flush = () => {
    flushList();
    flushQuote();
  };
  const listItem = (ordered: boolean, text: string) => {
    flushQuote();
    if (list && list.ordered !== ordered) flushList();
    list ??= { ordered, items: [] };
    list.items.push(inline(text));
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]!;

    if (/^```(\w*)\s*$/.test(line)) {
      flush();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i]!)) {
        code.push(lines[i]!);
        i += 1;
      }
      blocks.push(
        <pre key={blocks.length}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    if (TABLE_ROW.test(line) && TABLE_SEPARATOR.test(lines[i + 1] ?? "")) {
      flush();
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i]!)) {
        rows.push(cells(lines[i]!));
        i += 1;
      }
      i -= 1;
      blocks.push(
        // A wide table scrolls inside its own box, never the page.
        <div className="handout-text-table" key={blocks.length}>
          <table>
            <thead>
              <tr>
                {head.map((cell, index) => (
                  <th key={index} scope="col">
                    {inline(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, at) => (
                <tr key={at}>
                  {row.map((cell, index) => (
                    <td key={index}>{inline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const heading = /^(#{1,3}) (.+)$/.exec(line);
    if (heading) {
      flush();
      // A handout already has a title, so a top-level heading is not shown.
      const depth = heading[1]!.length;
      if (depth > 1)
        blocks.push(
          <Heading key={blocks.length} level={deeper(headingLevel, depth - 2)}>
            {inline(heading[2]!)}
          </Heading>,
        );
    } else if (/^- (.+)$/.test(line)) {
      listItem(false, line.replace(/^- /, ""));
    } else if (/^\d+\.\s+(.+)$/.test(line)) {
      listItem(true, line.replace(/^\d+\.\s+/, ""));
    } else if (/^> (.+)$/.test(line)) {
      flushList();
      quote ??= [];
      quote.push(inline(line.replace(/^> /, "")));
    } else {
      flush();
      if (line.trim() !== "")
        blocks.push(<p key={blocks.length}>{inline(line)}</p>);
    }
  }
  flush();
  return blocks;
}
