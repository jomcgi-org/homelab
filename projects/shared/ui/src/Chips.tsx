import type { ReactNode } from "react";

/** A wrapping row of chips. */
export function Chips({
  label,
  children,
}: {
  /** The accessible name of the list, when it needs one. */
  label?: string;
  children: ReactNode;
}) {
  return (
    <ul className="ui-chips" aria-label={label}>
      {children}
    </ul>
  );
}

/** One chip. With `href` it is a link; `current` marks the selected one. */
export function Chip({
  href,
  current = false,
  children,
}: {
  href?: string;
  current?: boolean;
  children: ReactNode;
}) {
  return (
    <li>
      {href === undefined ? (
        <span className="ui-chip">{children}</span>
      ) : (
        <a
          className="ui-chip"
          href={href}
          aria-current={current ? "page" : undefined}
        >
          {children}
        </a>
      )}
    </li>
  );
}
