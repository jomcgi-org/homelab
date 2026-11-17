import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./palette.css";
import "./site.css";
import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";

export function mount(page: ReactNode) {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <Theme theme={neutralTheme}>{page}</Theme>
    </StrictMode>,
  );
}
