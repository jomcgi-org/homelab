import { StrictMode, type ReactNode } from "react";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";

/**
 * The tree every page renders in: at build time on the server (prerender.tsx)
 * and in the browser (mount.tsx). Both must match, or hydration fails.
 */
export function Root({ children }: { children: ReactNode }) {
  return (
    <StrictMode>
      <Theme theme={neutralTheme}>{children}</Theme>
    </StrictMode>
  );
}
