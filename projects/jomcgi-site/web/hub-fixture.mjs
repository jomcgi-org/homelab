// Test-only access to the committed hub data and GET handler. Imports are
// supplied by the caller's file list, without loading the Svelte runtime.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const hub = new URL("../../monolith/frontend/src/", import.meta.url);
export const readHub = (path) => readFileSync(new URL(path, hub), "utf8");

export function evaluateHub(files, expression) {
  const script = files
    .map(readHub)
    .join("\n")
    .replace(/^import[\s\S]*?;\n/gm, "")
    .replace(/^export /gm, "");
  return vm.runInNewContext(`${script}\n${expression}`, { Response });
}
