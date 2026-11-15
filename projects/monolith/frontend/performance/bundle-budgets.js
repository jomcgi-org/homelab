import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";

const scoped = (id) =>
  id.startsWith("/public/app/") ||
  id === "/public/ember/firecracker" ||
  id === "/public/scotland" ||
  id.startsWith("/public/scotland/");

function sumFiles(files, readClient) {
  let bytes = 0;
  let gzip = 0;
  for (const file of files) {
    const body = readClient(file);
    bytes += body.length;
    gzip += gzipSync(body, { level: 9 }).length;
  }
  return { bytes, gzip, files: [...files].sort() };
}

// Kit emits each node's complete static preload closure. Read that declaration
// without importing server page modules or invoking their loaders.
export function nodeImports(source) {
  const match = source.match(/export const imports = (\[[^;]*\]);/);
  if (!match) throw new Error("SvelteKit node imports declaration is missing");
  const imports = JSON.parse(match[1]);
  if (
    !Array.isArray(imports) ||
    imports.some((file) => typeof file !== "string")
  )
    throw new Error("SvelteKit node imports declaration is invalid");
  return imports;
}

export function measureRoutes({
  manifest,
  viteManifest,
  readNode,
  readClient,
}) {
  if (!Array.isArray(manifest?._?.routes) || !manifest?._?.client?.imports)
    throw new Error("SvelteKit client route manifest is missing");
  const byFile = new Map(
    Object.entries(viteManifest).map(([key, item]) => [item.file, key]),
  );
  const rows = [];
  for (const route of manifest._.routes) {
    if (!route.page || !scoped(route.id)) continue;
    const initial = new Set(manifest._.client.imports);
    const roots = new Set();
    for (const index of [...route.page.layouts, route.page.leaf]) {
      if (index == null) continue;
      const imports = nodeImports(readNode(index));
      if (imports.length) roots.add(imports[0]);
      for (const file of imports) initial.add(file);
    }

    const deferred = new Set();
    const visited = new Set();
    function visit(key) {
      if (visited.has(key)) return;
      visited.add(key);
      const item = viteManifest[key];
      if (!item) throw new Error(`Missing Vite import: ${key}`);
      if (!initial.has(item.file)) deferred.add(item.file);
      for (const dependency of [
        ...(item.imports || []),
        ...(item.dynamicImports || []),
      ]) {
        visit(dependency);
      }
    }
    // The application entry's dynamic imports include every route. Only the
    // selected page/layout roots contribute deferred features to this route.
    for (const file of roots) visit(byFile.get(file));

    const workers = new Set();
    for (const file of [...initial, ...deferred]) {
      // Vite's ?worker&url wrapper contains the emitted worker URL, but its
      // manifest omits that asset. Account for these separately from imports.
      const source = readClient(file).toString();
      for (const match of source.matchAll(
        /["']\/?([^"']+\/workers\/[^"']+\.(?:js|mjs))["']/g,
      )) {
        const reference = match[1];
        const worker = reference.startsWith(".")
          ? path.posix.normalize(
              path.posix.join(path.posix.dirname(file), reference),
            )
          : reference;
        if (worker.startsWith("../") || path.posix.isAbsolute(worker))
          throw new Error(
            `Worker URL escapes the client directory: ${reference}`,
          );
        workers.add(worker);
      }
    }
    const eagerHeavy = [...initial].filter((file) =>
      ["maplibre-gl", "mermaid", "mermaid.core"].includes(
        viteManifest[byFile.get(file)]?.name,
      ),
    );
    rows.push({
      route: route.id,
      initial: sumFiles(initial, readClient),
      deferred: sumFiles(deferred, readClient),
      workers: sumFiles(workers, readClient),
      eagerHeavy,
    });
  }
  return rows;
}

export function enforceBudgets(rows, budgets, routes, readDocument) {
  const errors = [];
  const measured = new Set(rows.map((row) => row.route));
  const byRoute = new Map(routes.map((route) => [route.id, route]));
  const apiEndpoints = new Set(budgets.apiEndpoints);
  for (const route of routes) {
    if (!scoped(route.id) || !route.endpoint) continue;
    const isApi = apiEndpoints.has(route.id);
    const isDocument = Object.hasOwn(budgets.documents, route.id);
    if (!isApi && !isDocument)
      errors.push(`${route.id}: classify endpoint as API or budgeted document`);
    if (isApi && isDocument)
      errors.push(
        `${route.id}: endpoint has both API and document classifications`,
      );
  }
  for (const route of apiEndpoints) {
    if (!byRoute.get(route)?.endpoint)
      errors.push(`${route}: API classification has no built endpoint`);
  }
  for (const route of Object.keys(budgets.routes)) {
    if (!measured.has(route)) errors.push(`${route}: budget has no built page`);
  }
  for (const row of rows) {
    const budget = budgets.routes[row.route];
    if (!budget) {
      errors.push(`${row.route}: add an explicit bundle budget`);
      continue;
    }
    for (const [kind, key] of [
      ["initial", "initialGzip"],
      ["deferred", "deferredGzip"],
      ["workers", "workerGzip"],
    ]) {
      if (!Number.isSafeInteger(budget[key]) || budget[key] < 0)
        errors.push(`${row.route}: invalid ${key} budget`);
      else if (row[kind].gzip > budget[key])
        errors.push(
          `${row.route}: ${kind} JS ${row[kind].gzip} gzip bytes exceeds ${budget[key]}`,
        );
    }
    if (row.eagerHeavy.length)
      errors.push(
        `${row.route}: heavy library in initial JS: ${row.eagerHeavy}`,
      );
  }
  const documents = [];
  for (const [route, budget] of Object.entries(budgets.documents)) {
    if (!byRoute.get(route)?.endpoint)
      errors.push(`${route}: document classification has no built endpoint`);
    const body = readDocument(budget.file);
    const gzip = gzipSync(body, { level: 9 }).length;
    documents.push({ route, bytes: body.length, gzip, limit: budget.gzip });
    if (!Number.isSafeInteger(budget.gzip) || gzip > budget.gzip)
      errors.push(`${route}: HTML ${gzip} gzip bytes exceeds ${budget.gzip}`);
  }
  if (errors.length)
    throw new Error(`Public app bundle budgets failed:\n${errors.join("\n")}`);
  return documents;
}

export async function checkBundleBudgets(builder, out) {
  const server = builder.getServerDirectory();
  const client = builder.getClientDirectory();
  const { manifest } = await import(
    pathToFileURL(path.join(server, "manifest-full.js")).href
  );
  const budgets = JSON.parse(
    readFileSync(new URL("./bundle-budgets.json", import.meta.url), "utf8"),
  );
  const rows = measureRoutes({
    manifest,
    viteManifest: JSON.parse(
      readFileSync(path.join(client, ".vite/manifest.json"), "utf8"),
    ),
    readNode: (index) =>
      readFileSync(path.join(server, `nodes/${index}.js`), "utf8"),
    readClient: (file) => readFileSync(path.join(client, file)),
  });
  const documents = enforceBudgets(rows, budgets, manifest._.routes, (file) =>
    readFileSync(file),
  );
  builder.mkdirp(out);
  writeFileSync(
    path.join(out, "bundle-budgets.json"),
    JSON.stringify(
      { compression: "gzip level 9 per file", rows, documents },
      null,
      2,
    ),
  );
  builder.log.info(`Public app bundle budgets passed for ${rows.length} pages`);
}
