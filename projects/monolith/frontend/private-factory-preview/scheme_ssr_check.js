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
  const { default: SchemeToggle } = await server.ssrLoadModule(
    "/src/lib/public/components/SchemeToggle.svelte",
  );
  const { fixtureData, routes } = await server.ssrLoadModule(
    "/private-factory-preview/fixtures.js",
  );
  const { page } = await server.ssrLoadModule(
    "/private-factory-preview/state.js",
  );
  const records = [];
  // Render independent documents repeatedly so context cannot leak across SSR
  // requests. Public toggles must still bootstrap after a private layout render.
  for (const [view, path] of [
    ["public-toggle", "/public"],
    ...Object.entries(routes),
    ["public-toggle", "/public"],
  ]) {
    page.set({
      url: new URL(`https://private.example.test${path}`),
      data: {},
      error: null,
    });
    const result =
      view === "public-toggle"
        ? render(SchemeToggle)
        : render(App, { props: { view, data: fixtureData(view) } });
    const scripts = [
      ...result.head.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g),
    ]
      .map((match) => match[1])
      .filter((script) => script.includes('localStorage.getItem("td-theme")'));
    assert.equal(scripts.length, 1, `${view}: SSR preference scripts`);
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
