import { useEffect, useRef, type ReactNode } from "react";

/** A menu of links under one label.
 *
 * It is a native <details>: every link is in the HTML and the menu opens with
 * no script. Script only closes it on Escape or a press outside it. */
export function Menu({
  label,
  children,
}: {
  label: string;
  /** <li> elements. */
  children: ReactNode;
}) {
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
    <details className="ui-menu" ref={menu}>
      <summary>{label}</summary>
      <ul>{children}</ul>
    </details>
  );
}
