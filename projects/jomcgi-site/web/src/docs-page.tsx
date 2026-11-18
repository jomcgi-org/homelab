import "./docs-diagrams.css";
import {
  DocsShell,
  DocsTabs,
  type DocTab,
  type DocHeading,
} from "./docs-shell";

export interface DocData {
  slug: string;
  title: string;
  project: string;
  kind: string;
  html: string;
  toc: DocHeading[];
  tabs: DocTab[];
}

export function DocsPage({ data }: { data: DocData }) {
  return (
    <DocsShell activeSlug={data.slug} toc={data.toc}>
      <article aria-labelledby="doc-h">
        <header>
          <p>{data.project}</p>
          <DocsTabs tabs={data.tabs} activeKind={data.kind} />
          <h1 id="doc-h">{data.title}</h1>
        </header>
        <div
          id="doc-body"
          className="docs-prose"
          dangerouslySetInnerHTML={{ __html: data.html }}
        />
      </article>
    </DocsShell>
  );
}
