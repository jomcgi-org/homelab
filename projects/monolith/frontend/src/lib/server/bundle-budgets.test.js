import { describe, expect, it } from "vitest";
import { gzipSync } from "node:zlib";
import {
  enforceBudgets,
  measureRoutes,
  nodeImports,
} from "../../../performance/bundle-budgets.js";

const route = "/public/app/hikes";
const files = {
  "entry.js": "import('./unrelated.js');",
  "shared.js": "const shared = 'shared';",
  "layout.js": "import './shared.js';",
  "page.js": "import './shared.js'; import('./map.js');",
  "map.js": 'const worker = "/_app/immutable/workers/map.js";',
  "map-support.js": "export const workerSupport = true;",
  "_app/immutable/workers/map.js": "onmessage = () => {};",
  "unrelated.js": "const unrelated = 'x'.repeat(100000);",
};
const readClient = (file) => {
  if (!(file in files)) throw new Error(`Missing file: ${file}`);
  return Buffer.from(files[file]);
};
const fixture = () => ({
  manifest: {
    _: {
      client: { imports: ["entry.js", "shared.js"] },
      routes: [
        { id: route, page: { layouts: [0, undefined], leaf: 1 } },
        { id: "/private/example", page: { layouts: [0], leaf: 2 } },
      ],
    },
  },
  viteManifest: {
    entry: { file: "entry.js", dynamicImports: ["unrelated"] },
    shared: { file: "shared.js" },
    layout: { file: "layout.js", imports: ["shared"] },
    page: {
      file: "page.js",
      imports: ["shared"],
      dynamicImports: ["map"],
    },
    map: {
      file: "map.js",
      name: "maplibre-gl",
      imports: ["mapSupport", "shared"],
    },
    mapSupport: { file: "map-support.js", imports: ["map"] },
    unrelated: { file: "unrelated.js" },
  },
  readNode: (index) =>
    `export const imports = ${JSON.stringify(
      index === 0 ? ["layout.js", "shared.js"] : ["page.js", "shared.js"],
    )};`,
  readClient,
});

const budgets = () => ({
  routes: {
    [route]: { initialGzip: 1000, deferredGzip: 1000, workerGzip: 1000 },
  },
  redirects: [],
  documents: {},
});

describe("public route bundle measurement", () => {
  it("counts shared chunks once and follows only the selected route's lazy features", () => {
    const [row] = measureRoutes(fixture());
    expect(row.route).toBe(route);
    expect(row.initial.files).toEqual([
      "entry.js",
      "layout.js",
      "page.js",
      "shared.js",
    ]);
    expect(row.deferred.files).toEqual(["map-support.js", "map.js"]);
    expect(row.workers.files).toEqual(["_app/immutable/workers/map.js"]);
    expect(row.initial.gzip).toBe(
      row.initial.files.reduce(
        (sum, file) => sum + gzipSync(readClient(file), { level: 9 }).length,
        0,
      ),
    );
    expect(row.eagerHeavy).toEqual([]);
  });

  it("rejects a missing Vite dependency instead of undercounting it", () => {
    const input = fixture();
    input.viteManifest.page.imports.push("missing");
    expect(() => measureRoutes(input)).toThrow("Missing Vite import: missing");
  });

  it("resolves emitted worker URLs relative to their importing chunk", () => {
    const input = fixture();
    const source = input.readClient;
    input.readClient = (file) =>
      file === "map.js"
        ? Buffer.from(
            'const worker = new URL("./_app/immutable/workers/map.js", import.meta.url);',
          )
        : source(file);
    expect(measureRoutes(input)[0].workers.files).toEqual([
      "_app/immutable/workers/map.js",
    ]);
  });

  it("fails closed when SvelteKit changes its generated declarations", () => {
    expect(() => nodeImports("export const somethingElse = [];")).toThrow(
      "imports declaration is missing",
    );
    expect(nodeImports("export const imports = [];")).toEqual([]);
    expect(() => nodeImports("export const imports = [1];")).toThrow("invalid");
    expect(() => measureRoutes({ ...fixture(), manifest: {} })).toThrow(
      "route manifest is missing",
    );
  });
});

describe("public route bundle budgets", () => {
  it.each([
    ["initialGzip", "initial"],
    ["deferredGzip", "deferred"],
    ["workerGzip", "workers"],
  ])("fails the build when %s is exceeded", (key, label) => {
    const input = fixture();
    const limits = budgets();
    limits.routes[route][key] = 1;
    expect(() =>
      enforceBudgets(measureRoutes(input), limits, input.manifest._.routes),
    ).toThrow(`${route}: ${label} JS`);
  });

  it("catches moving MapLibre into initial imports even under the byte limit", () => {
    const input = fixture();
    const original = input.readNode;
    input.readNode = (index) => {
      const imports = nodeImports(original(index));
      if (index === 1) imports.push("map.js", "map-support.js");
      return `export const imports = ${JSON.stringify(imports)};`;
    };
    expect(() =>
      enforceBudgets(measureRoutes(input), budgets(), input.manifest._.routes),
    ).toThrow("heavy library in initial JS");
  });

  it("requires a budget for a new page and detects an orphaned budget", () => {
    const input = fixture();
    const rows = measureRoutes(input);
    expect(() =>
      enforceBudgets(
        rows,
        { ...budgets(), routes: {} },
        input.manifest._.routes,
      ),
    ).toThrow("add an explicit bundle budget");
    expect(() => enforceBudgets([], budgets(), [])).toThrow(
      "budget has no built page",
    );
  });

  it("checks standalone HTML including its inline scripts", () => {
    const input = fixture();
    const limits = budgets();
    limits.documents["/public/app/frank"] = { file: "frank.html", gzip: 1 };
    const routes = [...input.manifest._.routes, { id: "/public/app/frank" }];
    expect(() =>
      enforceBudgets(measureRoutes(input), limits, routes, () =>
        Buffer.from("<script>const inline = 123;</script>"),
      ),
    ).toThrow("/public/app/frank: HTML");
  });
});
