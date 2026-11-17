import { Fragment, type ReactNode } from "react";

const TOKEN = /\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\)/g;

/**
 * Renders the two inline forms the CV text uses, **bold** and [text](url),
 * without a markdown dependency. Everything else stays plain text.
 */
export function Inline({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[1] !== undefined) {
      out.push(<strong key={match.index}>{match[1]}</strong>);
    } else {
      out.push(
        <a key={match.index} href={match[3]} target="_blank" rel="noreferrer">
          {match[2]}
        </a>,
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <Fragment>{out}</Fragment>;
}
