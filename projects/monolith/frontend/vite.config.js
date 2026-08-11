import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [sveltekit()],
  resolve: {
    // Svelte 5's package.json `exports` uses `browser` for the client
    // bundle and `default` for the server. If `browser` isn't in the
    // resolved conditions, you get `index-server.js` on the client —
    // onMount becomes a noop and onDestroy crashes accessing
    // ssr_context.r. Pinning the conditions explicitly avoids that.
    conditions: ["browser", "module", "import", "default"],
  },
  build: {
    target: "es2022",
  },
  ssr: {
    // The runtime image ships only the SvelteKit output dir (remapped to
    // /app/build at image time); there is no node_modules. Any package
    // imported by SSR-rendered code must be bundled into the server chunks,
    // not externalized.
    noExternal: [
      "@dagrejs/dagre",
      "d3-quadtree",
      "d3-selection",
      "d3-zoom",
      "marked",
    ],
  },
  server: {
    proxy: {
      "/api": process.env.API_BASE || "http://localhost:8000",
    },
  },
});
