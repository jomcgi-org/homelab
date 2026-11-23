import { useEffect, useRef, type ReactNode } from "react";
import { GROUPS, hrefFor, sectionsIn } from "./sections";

/** A group with more links than this folds into one menu. */
const MENU_FROM = 3;

/* The menu is a native <details>: every link is in the HTML and it opens
 * with no script. Script only adds closing on Escape or an outside press. */
function Menu({ label, children }: { label: string; children: ReactNode }) {
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: Event) => {
      const details = menu.current;
      if (!details?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
        details.querySelector("summary")?.focus();
      } else if (details.contains(event.target as Node)) return;
      details.open = false;
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  return (
    <details className="site-menu" ref={menu}>
      <summary>{label}</summary>
      <ul>{children}</ul>
    </details>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="site">
      <header className="site-header">
        <a className="site-brand" href="/">
          jomcgi.dev
        </a>
        <nav aria-label="Site">
          <ul className="site-nav">
            {GROUPS.map((group) => {
              const links = sectionsIn(group).map((section) => (
                <li key={section.path}>
                  <a href={hrefFor(section)}>{section.name}</a>
                </li>
              ));
              return links.length > MENU_FROM ? (
                <li key={group}>
                  <Menu label={group}>{links}</Menu>
                </li>
              ) : (
                links
              );
            })}
          </ul>
        </nav>
      </header>
      <main className="site-main">{children}</main>
    </div>
  );
}
