/**
 * Fixture preview: the real app against the in-memory backend in
 * src/test/world.ts. The route is the URL hash, so any screen opens directly:
 * index.html#/campaigns/<id>/session
 */
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "../src/theme/sheet.css";
import "../src/app/app.css";

import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { App } from "../src/App";
import { Providers } from "../src/app/Providers";
import { installBackend } from "../src/test/backend";
import { world } from "../src/test/world";

installBackend(world);

createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={[location.hash.slice(1) || "/"]}>
    <Providers>
      <App />
    </Providers>
  </MemoryRouter>,
);
