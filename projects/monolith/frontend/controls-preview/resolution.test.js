import { expect, it } from "vitest";
import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";
import { realpath } from "node:fs/promises";

it("resolves barrel and direct exports from the frontend with a single Svelte runtime", async () => {
  const server = await createServer({
    configFile: false,
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    resolve: { conditions: ["browser"], dedupe: ["svelte"] },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null },
    appType: "custom",
  });
  try {
    const importer = resolve("controls-preview/ControlsFixture.svelte");
    const barrel = await server.pluginContainer.resolveId(
      "@homelab/design-system/components",
      importer,
    );
    expect(barrel.id).toMatch(/\/design-system\/components\/index.js$/);
    for (const name of [
      "Button",
      "Field",
      "Disclosure",
      "Tabs",
      "PageHeader",
      "Breadcrumb",
    ]) {
      const direct = await server.pluginContainer.resolveId(
        `@homelab/design-system/components/${name}.svelte`,
        importer,
      );
      expect(direct.id).toMatch(
        new RegExp(`/design-system/components/${name}\\.svelte$`),
      );
      for (const runtime of ["svelte", "svelte/internal/client"]) {
        const fromFixture = await server.pluginContainer.resolveId(
          runtime,
          importer,
        );
        const fromComponent = await server.pluginContainer.resolveId(
          runtime,
          direct.id,
        );
        expect(await realpath(fromComponent.id)).toBe(
          await realpath(fromFixture.id),
        );
      }
    }
  } finally {
    await server.close();
  }
});
