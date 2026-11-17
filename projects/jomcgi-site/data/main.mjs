// Entry point of the data snapshot (HOME-209), run by run.sh before each
// deploy. It always runs; snapshot.mjs holds the logic the tests import.
// Env: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (read-only R2 token).
// Usage: snapshot <output directory>
import { readFileSync } from "node:fs";
import { r2Getter, snapshot } from "./snapshot.mjs";

const out = process.argv[2];
if (!out) throw new Error("usage: snapshot <output directory>");
const accessKeyId = process.env.R2_ACCESS_KEY_ID;
const secretAccessKey = process.env.R2_SECRET_ACCESS_KEY;
if (!accessKeyId || !secretAccessKey) {
  throw new Error("R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must be set");
}
const sources = JSON.parse(
  readFileSync(new URL("./sources.json", import.meta.url), "utf8"),
);
const get = r2Getter(sources.endpoint, { accessKeyId, secretAccessKey });
for (const line of await snapshot(sources, get, out)) console.log(line);
