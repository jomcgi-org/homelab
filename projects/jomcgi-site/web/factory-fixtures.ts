import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type { Plugin } from "vite";

/** Only the development server serves invented data. Builds contain none. */
export function factoryFixtures(): Plugin {
  const fixtures = resolve(process.cwd(), "../../factory-storyboard/fixtures");
  return {
    name: "factory-fixtures",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/data/factory", async (request, response) => {
        const path = request.url?.split("?")[0] ?? "";
        response.setHeader("Content-Type", "application/json; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        if (/^\/(manifest\.json|objects\/[a-f0-9]{64}\.json)$/.test(path)) {
          try {
            response.end(await readFile(resolve(fixtures, path.slice(1))));
            return;
          } catch {
            /* A missing object is an ordinary unavailable state. */
          }
        }
        response.statusCode = 404;
        response.end(JSON.stringify({ error: "Not available" }));
      });
      // Match public/_redirects, while leaving the HTML entry itself to Vite.
      server.middlewares.use((request, _response, next) => {
        const path = request.url?.split("?")[0] ?? "";
        if (path === "/factory" || path.startsWith("/factory/")) {
          request.url = "/factory/index.html";
        }
        next();
      });
    },
  };
}
