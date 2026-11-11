import { useMemo } from "react";

/**
 * Tracks an IME composition inside an element. While Japanese, Chinese or
 * Korean text is being composed the input holds an unfinished word, so a
 * search waits for `settled()` and then runs once on what was committed.
 *
 *   const composition = useComposition();
 *   <div {...composition.handlers}><Typeahead ... /></div>
 */
export function useComposition() {
  return useMemo(() => {
    let composing = false;
    let waiting: Array<() => void> = [];
    return {
      handlers: {
        onCompositionStart: () => {
          composing = true;
        },
        onCompositionEnd: () => {
          composing = false;
          waiting.forEach((resume) => resume());
          waiting = [];
        },
      },
      /** Resolves at once, or when the composition in progress ends. */
      settled: (): Promise<void> =>
        composing
          ? new Promise((resume) => waiting.push(resume))
          : Promise.resolve(),
    };
  }, []);
}
