// Run after editing or formatting an object. Hash exactly the bytes on disk;
// keep manifest formatting intact and never rewrite an object's contents.
import { createHash, randomUUID } from "node:crypto";
import {
  readFile,
  readdir,
  rename,
  rm,
  unlink,
  writeFile,
} from "node:fs/promises";

const root = new URL("./", import.meta.url);
const manifestURL = new URL("manifest.json", root);
const manifest = await readFile(manifestURL, "utf8");
JSON.parse(manifest);
const names = new Set(await readdir(new URL("objects/", root)));
const plan = [];
for (const name of [...names].sort()) {
  if (!name.endsWith(".json")) continue;
  const bytes = await readFile(new URL(`objects/${name}`, root));
  const next = `${createHash("sha256").update(bytes).digest("hex")}.json`;
  plan.push({ name, next, bytes });
}

// Read every input and reject conflicts before writing or removing anything.
// Even a destination scheduled to move may contain another uncommitted edit.
const destinations = new Set();
for (const { name, next } of plan) {
  if (destinations.has(next) || (name !== next && names.has(next))) {
    throw new Error(`Rehash conflict: ${name} would replace or share ${next}`);
  }
  destinations.add(next);
}
const replacements = new Map(
  plan.map(({ name, next }) => [`objects/${name}`, `objects/${next}`]),
);
// Replace complete JSON strings in one pass, preserving manifest formatting.
const updated = manifest.replace(/"(?:[^"\\]|\\.)*"/g, (quoted) => {
  const next = replacements.get(JSON.parse(quoted));
  return next === undefined ? quoted : JSON.stringify(next);
});
const changed = plan.filter(({ name, next }) => name !== next);
for (const { next, bytes } of changed) {
  // Exclusive creation also protects a destination created after the check.
  await writeFile(new URL(`objects/${next}`, root), bytes, { flag: "wx" });
}
// Replace the manifest by rename so a failed write leaves the original intact.
const staged = new URL(`.manifest.json.${randomUUID()}.tmp`, root);
try {
  await writeFile(staged, updated, { flag: "wx" });
  await rename(staged, manifestURL);
} catch (error) {
  await rm(staged, { force: true });
  throw error;
}
// Originals survive any failure while creating the new objects or manifest.
for (const { name } of changed) {
  await unlink(new URL(`objects/${name}`, root));
}
