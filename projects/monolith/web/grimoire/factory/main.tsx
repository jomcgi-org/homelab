import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "../src/theme/sheet.css";
import "../src/app/app.css";
import "./page.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Theme } from "@astryxdesign/core/theme";
import { sheetTheme } from "../src/theme";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Theme theme={sheetTheme}>
      <App />
    </Theme>
  </StrictMode>,
);
