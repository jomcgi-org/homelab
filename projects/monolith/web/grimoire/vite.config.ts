import { defineConfig } from "vite";

// The app is static files served under one path prefix on the friends lane.
// Assets live under the same prefix, so they stay on the route whose
// SecurityPolicy gates every byte. grimoire/web.py reads the prefix back out
// of the built index.html, so only this value sets where the app lives.
export const base = process.env.GRIMOIRE_BASE || "/grimoire/";

export default defineConfig({
  base,
  // esbuild compiles TSX with the automatic runtime; no React plugin needed.
  esbuild: { jsx: "automatic" },
  build: {
    outDir: "dist",
    // Astryx targets current browsers; the default target cannot lower its output.
    target: "es2022",
    // The Content-Security-Policy allows no inline script or style.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    sourcemap: false,
    rollupOptions: {
      // Libraries mark client components for React Server Components. This
      // app has no server, so the directive means nothing here.
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
  server: {
    proxy: { "/api": process.env.API_BASE || "http://localhost:8000" },
  },
});
