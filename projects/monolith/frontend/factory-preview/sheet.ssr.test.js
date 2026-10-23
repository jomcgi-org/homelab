// @vitest-environment node
import { expect, it } from "vitest";
import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { runInNewContext } from "node:vm";

it("server-renders the same before-paint bootstrap for the button and private journal", async () => {
  const server = await createServer({
    configFile: false,
    root: process.cwd(),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule(
      "/factory-preview/sheet-scheme-server.js",
    );
    const { preference, toggle } = entry.renderSchemes();
    const script = (head) => head.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
    const bootstrap = script(preference.head);
    expect(script(toggle.head)).toBe(bootstrap);
    expect(preference.body).not.toContain("button");
    expect(toggle.body).toContain('class="scheme');
    for (const value of ["light", "dark", null, "invalid"]) {
      const document = { documentElement: { dataset: {} } };
      runInNewContext(bootstrap, {
        document,
        localStorage: { getItem: () => value },
      });
      expect(document.documentElement.dataset.theme).toBe(
        ["light", "dark"].includes(value) ? value : undefined,
      );
    }
    expect(() =>
      runInNewContext(bootstrap, {
        localStorage: {
          getItem: () => {
            throw new Error("Storage unavailable");
          },
        },
      }),
    ).not.toThrow();
  } finally {
    await server.close();
  }
});
