import type { ReactNode } from "react";

/** How far along a thing is. The mark is a small square; the word beside it
 * carries the meaning, so colour is never the only signal. */
export type Tone = "done" | "active" | "started" | "idle";

export function StateMark({
  tone,
  children,
}: {
  tone: Tone;
  children: ReactNode;
}) {
  return (
    <span className="ui-state" data-tone={tone}>
      {children}
    </span>
  );
}
