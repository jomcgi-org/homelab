// Run under Node's server conditions, separately from the browser DOM tests.
import assert from "node:assert/strict";
import { runInNewContext } from "node:vm";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { aliases } from "./vite.config.js";

const server = await createServer({
  configFile: false,
  plugins: [svelte({ configFile: false })],
  resolve: { alias: aliases, conditions: ["node"] },
  server: { middlewareMode: true },
  optimizeDeps: {
    noDiscovery: true,
    include: [],
    esbuildOptions: { target: "esnext" },
  },
});
try {
  const { render } = await server.ssrLoadModule("svelte/server");
  const { default: App } = await server.ssrLoadModule(
    "/private-factory-preview/App.svelte",
  );
  const { fixtureData } = await server.ssrLoadModule(
    "/private-factory-preview/fixtures.js",
  );
  const { page } = await server.ssrLoadModule(
    "/private-factory-preview/state.js",
  );
  const records = [];
  for (const [view, path, count] of [
    ["legacy", "/ordinary", 1],
    ["updates", "/updates", 2],
    ["board", "/factory", 2],
  ]) {
    page.set({
      url: new URL(`https://private.example.test${path}`),
      data: {},
      error: null,
    });
    const result = render(App, {
      props: { view, data: view === "legacy" ? {} : fixtureData(view) },
    });
    const scripts = [
      ...result.head.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g),
    ]
      .map((match) => match[1])
      .filter((script) => script.includes('localStorage.getItem("td-theme")'));
    // Reusing the component shares source, not head deduplication. Each consumer
    // emits an idempotent script; keep the layout's script for toggle-free pages.
    assert.equal(scripts.length, count, `${view}: SSR preference scripts`);
    assert.ok(!result.body.includes('localStorage.getItem("td-theme")'));
    if (process.argv[2])
      writeFileSync(
        join(process.argv[2], `scheme-${view}.html`),
        `<!doctype html><html><head>${result.head}</head><body><script>window.schemeBeforeBody = document.documentElement.dataset.theme ?? null;</script>${result.body}</body></html>`,
      );
    for (const stored of ["light", "dark", null, "invalid", "throws"]) {
      const document = { documentElement: { dataset: {} } };
      const sandbox = {
        document,
        localStorage: {
          getItem() {
            if (stored === "throws") throw new Error("Storage denied");
            return stored;
          },
        },
      };
      for (const script of scripts) runInNewContext(script, sandbox);
      assert.equal(
        document.documentElement.dataset.theme,
        ["light", "dark"].includes(stored) ? stored : undefined,
      );
    }
    records.push({
      view,
      scripts: scripts.length,
      storageCases: 5,
      passed: true,
    });
  }
  console.log(JSON.stringify(records));
} finally {
  await server.close();
}
