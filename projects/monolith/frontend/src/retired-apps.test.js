import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { apps } from "./lib/public/apps.js";

const paths = [
  "/app/dr-jobs",
  "/app/dr-jobs?view=history&town=Edinburgh",
  "/app/dr-jobs/",
  "/public/app/dr-jobs",
  "/app/campsites",
  "/app/campsites?park=fixture-park",
  "/app/campsites/",
  "/public/app/campsites",
];

// Exercise the built router and error boundary in plain Node, outside Vitest's
// Svelte browser aliases. The test target supplies the production :build.
const responses = JSON.parse(
  execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `
        import { readFileSync } from "node:fs";
        import { createServer, get } from "node:http";
        import { handler } from "./dist/handler.js";
        const server = createServer(handler);
        await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
        const fetches = [];
        globalThis.fetch = async (input) => {
          fetches.push(String(input));
          throw new Error("Retired pages must not fetch backend data");
        };
        const results = [];
        try {
          for (const path of JSON.parse(readFileSync(0, "utf8"))) {
            results.push(await new Promise((resolve, reject) => {
              get({
                hostname: "127.0.0.1",
                port: server.address().port,
                path,
                headers: { host: "jomcgi.dev", accept: "text/html" },
              }, (response) => {
                let html = "";
                response.setEncoding("utf8");
                response.on("data", (chunk) => html += chunk);
                response.on("error", reject);
                response.on("end", () => resolve({
                  path, html,
                  status: response.statusCode,
                  location: response.headers.location ?? null,
                }));
              }).on("error", reject);
            }));
          }
        } finally {
          await new Promise((resolve) => server.close(resolve));
        }
        console.log(JSON.stringify({ results, fetches }));
      `,
    ],
    {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      input: JSON.stringify(paths),
      encoding: "utf8",
      timeout: 20_000,
    },
  ),
);

describe("retired public apps", () => {
  it.each(responses.results)(
    "returns the standard 404 for $path",
    (response) => {
      expect(response.status).toBe(404);
      expect(response.location).toBeNull();
      expect(response.html).toContain("Error 404 / off the map");
      expect(response.html).toContain(
        new URL(response.path, "https://jomcgi.dev").pathname,
      );
      expect(response.html).toContain('name="robots" content="noindex"');
    },
  );

  it("does not fetch the retained backend services", () => {
    expect(responses.fetches).toEqual([]);
  });

  it("keeps retired pages out of the shared navigation registry", () => {
    const links = apps.map((app) => app.href);
    expect(links).not.toContain("/app/dr-jobs");
    expect(links).not.toContain("/app/campsites");
  });
});
