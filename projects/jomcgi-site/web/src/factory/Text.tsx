import type { ReactNode } from "react";

/* Issue and transcript text is written as Markdown. This renders the common
 * subset as React elements, so the source never becomes HTML. A link becomes
 * an anchor only when it points at this repository; elsewhere its text stays. */
const repository = "https://github.com/jomcgi-org/homelab/";
const inlinePattern =
  /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\s][^*\n]*)\*|\[([^\]\n]+)\]\((https:\/\/[^\s)]+)\)/g;

function inline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(inlinePattern)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const key = match.index;
    if (match[1] !== undefined) nodes.push(<code key={key}>{match[1]}</code>);
    else if (match[2] !== undefined)
      nodes.push(<strong key={key}>{inline(match[2])}</strong>);
    else if (match[3] !== undefined)
      nodes.push(<em key={key}>{inline(match[3])}</em>);
    else if (match[5]!.startsWith(repository))
      nodes.push(
        <a key={key} href={match[5]}>
          {match[4]}
        </a>,
      );
    else nodes.push(match[4]);
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

const itemPattern = /^(\s*)(?:([-*])|(\d+)[.)])\s+(.*)$/;
type Item = { indent: number; ordered: boolean; lines: string[] };

function list(items: Item[], key: number): ReactNode {
  const { indent, ordered } = items[0]!;
  const children: ReactNode[] = [];
  for (let index = 0; index < items.length;) {
    const item = items[index]!;
    let end = index + 1;
    while (end < items.length && items[end]!.indent > indent) end++;
    children.push(
      <li key={index}>
        {inline(item.lines.join("\n"))}
        {end > index + 1 && list(items.slice(index + 1, end), index)}
      </li>,
    );
    index = end;
  }
  return ordered ? (
    <ol key={key}>{children}</ol>
  ) : (
    <ul key={key}>{children}</ul>
  );
}

export function Text({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length;) {
    const line = lines[index]!;
    const key = index;
    if (!line.trim()) {
      index++;
    } else if (/^\s*```/.test(line)) {
      const code: string[] = [];
      for (index++; index < lines.length && !/^\s*```/.test(lines[index]!);)
        code.push(lines[index++]!);
      index++;
      blocks.push(<pre key={key}>{code.join("\n")}</pre>);
    } else if (/^#{1,6}\s/.test(line)) {
      blocks.push(
        <p key={key} className="text-heading">
          {inline(line.replace(/^#+\s+/, ""))}
        </p>,
      );
      index++;
    } else if (itemPattern.test(line)) {
      const items: Item[] = [];
      for (; index < lines.length && lines[index]!.trim(); index++) {
        const match = itemPattern.exec(lines[index]!);
        if (match)
          items.push({
            indent: match[1]!.length,
            ordered: match[3] !== undefined,
            lines: [match[4]!],
          });
        else items.at(-1)!.lines.push(lines[index]!.trim());
      }
      blocks.push(list(items, key));
    } else {
      const paragraph: string[] = [];
      for (
        ;
        index < lines.length &&
        lines[index]!.trim() &&
        !/^\s*```/.test(lines[index]!) &&
        !itemPattern.test(lines[index]!);
        index++
      )
        paragraph.push(lines[index]!);
      blocks.push(<p key={key}>{inline(paragraph.join("\n"))}</p>);
    }
  }
  return <div className="text">{blocks}</div>;
}
