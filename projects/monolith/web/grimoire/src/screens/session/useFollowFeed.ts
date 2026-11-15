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
export function useFollowFeed(count: number, active: boolean) {
  const seen = useRef(0);
  // Where the reader was before new entries grew the page. Measuring after
  // they arrive would count a tall entry as the reader having scrolled up.
  const following = useRef(true);
  const [behind, setBehind] = useState(0);

  useEffect(() => {
    if (!active) return;
    const track = () => {
      following.current = nearBottom();
      if (following.current) setBehind(0);
    };
    window.addEventListener("scroll", track, { passive: true });
    return () => window.removeEventListener("scroll", track);
  }, [active]);

  useEffect(() => {
    if (!active || count === seen.current) return;
    const first = seen.current === 0;
    const arrived = count - seen.current;
    seen.current = count;
    if (arrived <= 0) return;
    if (first || following.current) {
      // After layout, so the new entries are already in the page height.
      requestAnimationFrame(scrollToBottom);
      setBehind(0);
    } else {
      setBehind((held) => held + arrived);
    }
  }, [count, active]);

  const toLatest = useCallback(() => {
    scrollToBottom();
    following.current = true;
    setBehind(0);
  }, []);

  return { behind, toLatest };
}
