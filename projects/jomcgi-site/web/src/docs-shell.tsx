import type { ReactNode } from "react";
import sidebar from "../docs/_src/sidebar.json";
import { Shell } from "./Shell";
import { DocsSearch } from "./docs-search";
import "./docs.css";

export interface DocTab {
  kind: string;
  label: string;
  slug: string | null;
  title: string;
  disabled?: boolean;
}
export interface DocHeading {
  depth: number;
  text: string;
  id: string;
}

export function DocsTabs({
  tabs,
  activeKind = "",
  label = "Project documents",
}: {
  tabs: DocTab[];
  activeKind?: string;
  label?: string;
}) {
  return (
    <nav className="docs-tabs" aria-label={label}>
      {tabs
        .filter((tab) => tab.slug && !tab.disabled)
        .map((tab) => (
          <a
            key={tab.kind}
            href={`/docs/${tab.slug}`}
            title={tab.title}
            aria-current={activeKind === tab.kind ? "page" : undefined}
          >
            {tab.label}
          </a>
        ))}
    </nav>
  );
}

export function DocsShell({
  children,
  activeSlug = "",
  toc = [],
}: {
  children: ReactNode;
  activeSlug?: string;
  toc?: DocHeading[];
}) {
  return (
    <Shell>
      <a href="/docs">docs</a>
      <div className="docs-layout">
        <aside className="docs-sidebar">
          <DocsSearch />
          <nav aria-label="Projects">
            {sidebar.map((project) => (
              <details
                key={project.project}
                open={project.tabs.some((tab) => tab.slug === activeSlug)}
              >
                <summary>{project.title}</summary>
                <ul>
                  {project.tabs.map((tab) => (
                    <li key={tab.slug}>
                      <a
                        href={`/docs/${tab.slug}`}
                        title={tab.title}
                        aria-current={
                          tab.slug === activeSlug ? "page" : undefined
                        }
                      >
                        {tab.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </nav>
          {toc.length > 0 && (
            <details className="docs-toc">
              <summary>On this page</summary>
              <nav aria-label="On this page">
                <ul>
                  {toc.map((heading) => (
                    <li
                      key={heading.id}
                      className={
                        heading.depth === 3 ? "docs-subheading" : undefined
                      }
                    >
                      <a href={`#${heading.id}`}>{heading.text}</a>
                    </li>
                  ))}
                </ul>
              </nav>
            </details>
          )}
        </aside>
        <div className="docs-content">{children}</div>
      </div>
    </Shell>
  );
}
