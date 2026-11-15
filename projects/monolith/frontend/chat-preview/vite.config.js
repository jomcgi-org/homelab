import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";

const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: here("./"),
  publicDir: false,
  plugins: [svelte({ configFile: false })],
  resolve: {
    conditions: ["browser"],
    alias: { $lib: here("../src/lib") },
  },
  build: {
    target: "es2022",
    outDir: process.env.CHAT_PREVIEW_OUT ?? here("../chat-fixture-preview"),
    emptyOutDir: true,
  },
});
