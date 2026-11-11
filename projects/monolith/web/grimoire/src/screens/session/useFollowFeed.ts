import { useCallback, useEffect, useRef, useState } from "react";

/** How close to the bottom of the page still counts as reading the latest. */
const NEAR_PX = 240;

const nearBottom = () =>
  window.innerHeight + window.scrollY >=
  document.documentElement.scrollHeight - NEAR_PX;

const scrollToBottom = () =>
  window.scrollTo({ top: document.documentElement.scrollHeight });

/**
 * Keeps the newest feed entry in view. A reader at the bottom follows new
 * events; a reader who has scrolled up to re-read is left where they are and
 * told how many arrived, so the page never moves under them.
 */
export function useFollowFeed(lastSeq: number, active: boolean) {
  const seen = useRef(0);
  const [behind, setBehind] = useState(0);

  useEffect(() => {
    if (!active || lastSeq === 0 || lastSeq === seen.current) return;
    const first = seen.current === 0;
    const arrived = lastSeq - seen.current;
    seen.current = lastSeq;
    if (first || nearBottom()) {
      // After layout, so the new entries are already in the page height.
      requestAnimationFrame(scrollToBottom);
      setBehind(0);
    } else {
      setBehind((count) => count + arrived);
    }
  }, [lastSeq, active]);

  // Scrolling back down by hand clears the notice.
  useEffect(() => {
    if (behind === 0) return;
    const clear = () => {
      if (nearBottom()) setBehind(0);
    };
    window.addEventListener("scroll", clear, { passive: true });
    return () => window.removeEventListener("scroll", clear);
  }, [behind]);

  const toLatest = useCallback(() => {
    scrollToBottom();
    setBehind(0);
  }, []);

  return { behind, toLatest };
}
