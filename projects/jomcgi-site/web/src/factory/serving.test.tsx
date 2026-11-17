import { get } from "node:http";
import { afterAll, beforeAll, expect, it } from "vitest";
import { createServer, type ViteDevServer } from "vite";
import { factoryFixtures } from "../../factory-fixtures";
import { fixtureFiles, fixtureManifest } from "./test-fixtures";

let server: ViteDevServer;
let port: number;
beforeAll(async () => {
  server = await createServer({
    configFile: false,
    plugins: [factoryFixtures()],
    esbuild: { jsx: "automatic" },
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { host: "127.0.0.1", port: 0, hmr: false, watch: null },
  });
  await server.listen();
  const address = server.httpServer!.address();
  if (!address || typeof address === "string")
    throw new Error("Missing Vite port");
  port = address.port;
});
afterAll(async () => {
  await server?.close();
});

function request(
  path: string,
): Promise<{ status: number; type: string | undefined; body: string }> {
  return new Promise((resolve, reject) => {
    get({ host: "127.0.0.1", port, path }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () =>
        resolve({
          status: response.statusCode!,
          type: response.headers["content-type"],
          body,
        }),
      );
      response.on("error", reject);
    }).on("error", reject);
  });
}

it.each([
  "/factory",
  "/factory/",
  "/factory/goals/x",
  "/factory/issues/DEMO-104",
  "/factory/sessions/demo-build",
])("Vite serves the page shell at %s", async (path) => {
  const response = await request(path);
  expect(response.status).toBe(200);
  expect(response.body).toContain("/src/factory.tsx");
  expect(response.body).toContain('href="https://jomcgi.dev/factory"');
});

it("Vite serves exact fixture bytes on the site's data path", async () => {
  const files = fixtureFiles();
  for (const path of ["manifest.json", fixtureManifest().story]) {
    const url = `/data/factory/${path}`;
    const response = await request(url);
    expect(response.status).toBe(200);
    expect(response.type).toContain("application/json");
    expect(response.body).toBe(files.get(url));
  }
});

it.each([
  "missing.json",
  "../schema/types.ts",
  "objects/../../package.json",
  `objects/${"0".repeat(64)}.json`,
])("missing or invalid data %s stays a JSON 404", async (path) => {
  const response = await request(`/data/factory/${path}`);
  expect(response.status).toBe(404);
  expect(response.type).toContain("application/json");
  expect(JSON.parse(response.body)).toEqual({ error: "Not available" });
});
