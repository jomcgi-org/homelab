// Entry point of the edge apply Job (HOME-192). It always runs; apply.mjs
// holds the logic and is what the tests import.
// Env: CLOUDFLARE_API_TOKEN, and EDGE_SERVE_ROUTES "true" or "false" (the
// chart's edge.serveRoutes; "false" deletes the Worker's routes, the rollback).
// Usage: apply [--dry-run]
import { readFileSync } from "node:fs";
import { apply, client } from "./apply.mjs";

const read = (name) =>
  JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const token = process.env.CLOUDFLARE_API_TOKEN;
if (!token) throw new Error("CLOUDFLARE_API_TOKEN is not set");
const serve = process.env.EDGE_SERVE_ROUTES;
if (serve !== "true" && serve !== "false") {
  throw new Error('EDGE_SERVE_ROUTES must be "true" or "false"');
}
const log = await apply(
  client(token),
  read("./routes.json"),
  read("./cache-rules.json"),
  { dryRun: process.argv.includes("--dry-run"), serveRoutes: serve === "true" },
);
for (const line of log) console.log(line);
