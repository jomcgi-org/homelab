// Run after editing or formatting an object. Hash exactly the bytes on disk;
// keep manifest formatting intact and never rewrite an object's contents.
import { createHash } from "node:crypto";
import { readFile, readdir, rename, writeFile } from "node:fs/promises";

const root = new URL("./", import.meta.url);
const manifestURL = new URL("manifest.json", root);
let manifest = await readFile(manifestURL, "utf8");
for (const name of await readdir(new URL("objects/", root))) {
  if (!name.endsWith(".json")) continue;
  const file = new URL(`objects/${name}`, root);
  const hash = createHash("sha256").update(await readFile(file)).digest("hex");
  const next = `objects/${hash}.json`;
  manifest = manifest.replaceAll(`objects/${name}`, next);
  if (name !== `${hash}.json`) await rename(file, new URL(next, root));
}
await writeFile(manifestURL, manifest);
