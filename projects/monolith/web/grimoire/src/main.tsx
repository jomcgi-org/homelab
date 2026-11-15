import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "./theme/sheet.css";
import "./app/app.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App";
import { basename } from "./app/paths";
import { Providers } from "./app/Providers";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename={basename}>
      <Providers>
        <App />
      </Providers>
    </BrowserRouter>
  </StrictMode>,
);
