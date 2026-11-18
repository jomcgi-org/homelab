import type { ReactNode } from "react";
import "./diagrams.css";

// React ports of the hub's Diagram, DGroup, DBox and DArrow primitives.
// Labels stay as text; only the arrowheads need SVG.
export function Diagram({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <figure className="engineering-diagram" aria-label={label}>
      <figcaption>{label}</figcaption>
      <div className="diagram-art" tabIndex={0} role="group" aria-label={label}>
        <div className="diagram-flow">{children}</div>
      </div>
    </figure>
  );
}

export function DGroup({
  label,
  stack = false,
  children,
}: {
  label: string;
  stack?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="dgroup">
      <span className="dgroup-label">{label}</span>
      <div className={`dgroup-body${stack ? " stack" : ""}`}>{children}</div>
    </div>
  );
}

export function DBox({
  role = "process",
  sub,
  children,
}: {
  role?: "source" | "process" | "store" | "output" | "external";
  sub?: string;
  children: ReactNode;
}) {
  return (
    <span className={`dbox${role === "external" ? " external" : ""}`}>
      {children}
      {sub && <span className="dbox-sub">{sub}</span>}
    </span>
  );
}

export function DArrow({ label = "" }: { label?: string }) {
  return (
    <span className="darrow">
      <span className="darrow-label">{label}</span>
      <span className="shaft" aria-hidden="true">
        <span className="line" />
        <svg className="head" viewBox="0 0 8 12" width="8" height="12">
          <path
            d="M0.5,0.5 L7,6 L0.5,11.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="1"
            strokeLinejoin="miter"
          />
        </svg>
      </span>
      <span className="darrow-under" />
    </span>
  );
}
