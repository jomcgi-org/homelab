import { Text } from "@astryxdesign/core/Text";
import type { ReactNode } from "react";
import { GROUPS, hrefFor, sectionsIn } from "./sections";

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="site">
      <header className="site-header">
        <a className="site-brand" href="/">
          jomcgi.dev
        </a>
        <nav aria-label="Site">
          <ul className="site-nav">
            {GROUPS.map((group) => (
              <li key={group}>
                <span className="site-nav-group">{group}</span>
                <ul>
                  {sectionsIn(group).map((section) => (
                    <li key={section.path}>
                      <a href={hrefFor(section)}>{section.name}</a>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="site-main">{children}</main>
      <footer className="site-footer">
        <Text type="body">
          Static pages and data files, served from the edge.
        </Text>
      </footer>
    </div>
  );
}
