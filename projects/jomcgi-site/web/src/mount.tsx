import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./palette.css";
import "./site.css";
import type { ReactNode } from "react";
import { hydrateRoot } from "react-dom/client";
import { Root } from "./root";

/** Hydrates the HTML that build.mjs rendered from the same page. */
export function mount(page: ReactNode) {
  hydrateRoot(document.getElementById("root")!, <Root>{page}</Root>);
}
